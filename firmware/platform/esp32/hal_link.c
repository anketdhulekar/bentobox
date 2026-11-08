// The link to the desktop app (protocol/PROTOCOL.md): a Bluetooth LE peripheral named "Bento" with one service. One
// Mac at a time, up to four remembered. Only paired Macs can talk to it: pairing is LE Secure Connections with a 6-digit
// passkey that shows only on the screen, and only in pair mode. Bytes from the Mac go into a ring for hal_link_read.
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "nvs.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "store/config/ble_store_config.h"
#include "board_hw.h"
#include "hal.h"

static const char *TAG = "hal_link";

void ble_store_config_init(void);

#define BLE_NAME (BOARD.title ? BOARD.title : "Bento") // the desktop app finds a board by its service, not its name
#define NVS_MACS "macs"       // the Macs' names, by key, and the chosen Mac
#define NVS_CHOSEN "chosen"
// A link that isn't paired and encrypted by then is dropped, so nobody can sit on the one connection. A paired Mac
// encrypts within a second; pairing a new one takes the time to type its code.
#define SECURE_WITHIN_S 12
#define SECURE_PAIRING_S 60
#define KICK_DELAY_MS 400     // after the busy notification has gone out

// The service and its characteristics (128-bit UUIDs, least significant byte first):
//   service 62656e74-6f00-0000-0000-000000000000 ("bento" in ASCII)
//   RX      62656e74-6f00-0000-0000-000000000001  the Mac writes here
//   TX      62656e74-6f00-0000-0000-000000000002  the board notifies here
#define BENTO_UUID(last) BLE_UUID128_INIT(last, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x6f, 0x74, 0x6e, 0x65, 0x62)
static const ble_uuid128_t SVC_UUID = BENTO_UUID(0x00), RX_UUID = BENTO_UUID(0x01), TX_UUID = BENTO_UUID(0x02);

static uint8_t s_addr_type;
static uint16_t s_tx_handle;
static volatile uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static volatile bool s_secure, s_subscribed, s_pairing, s_new_pair;
static volatile uint32_t s_code;
static volatile int64_t s_pair_until; // esp_timer microseconds; pair mode is on before this
static char s_peer[16];               // the connected Mac's key (its identity address in hex): use peer_get and peer_set
static portMUX_TYPE s_peer_mux = portMUX_INITIALIZER_UNLOCKED; // the Bluetooth task writes it, and the UX's task reads it
static char s_chosen[16];             // "" = any Mac
static esp_timer_handle_t s_kick_timer, s_secure_timer;
static SemaphoreHandle_t s_send_lock;
static bool s_tx_broken;              // a send stopped inside a line, so the Mac holds half of it

static bool pair_mode(void) { return s_pair_until > esp_timer_get_time(); }

static void peer_set(const char *key)
{
    taskENTER_CRITICAL(&s_peer_mux);
    strlcpy(s_peer, key, sizeof s_peer);
    taskEXIT_CRITICAL(&s_peer_mux);
}

static void peer_get(char out[16])
{
    taskENTER_CRITICAL(&s_peer_mux);
    memcpy(out, s_peer, sizeof s_peer);
    taskEXIT_CRITICAL(&s_peer_mux);
}

static void addr_key(const ble_addr_t *a, char *out)
{
    snprintf(out, 16, "%02x%02x%02x%02x%02x%02x", a->val[5], a->val[4], a->val[3], a->val[2], a->val[1], a->val[0]);
}

// ---- Bytes from the Mac -------------------------------------------------------------------------------------

// A line is at most 16 KB, and the UX empties this every frame. It's in PSRAM. Only the Bluetooth host writes to it, and
// only the UX reads it.
#define RX_RING_BYTES (32 * 1024) // a power of two, so the counts wrap cleanly

static uint8_t *s_rx_ring;
static volatile uint32_t s_rx_head, s_rx_tail; // count bytes; the ring's position is the count modulo its size
static uint8_t s_rx_last = '\n';

static bool rx_put(const uint8_t *data, size_t n)
{
    const uint32_t head = s_rx_head, tail = __atomic_load_n(&s_rx_tail, __ATOMIC_ACQUIRE);
    if (!s_rx_ring || RX_RING_BYTES - (head - tail) < n) return false;
    for (size_t i = 0; i < n; i++) s_rx_ring[(head + i) % RX_RING_BYTES] = data[i];
    if (n) s_rx_last = data[n - 1];
    __atomic_store_n(&s_rx_head, head + (uint32_t)n, __ATOMIC_RELEASE);
    return true;
}

// A Mac that left in the middle of a line leaves half of one. Ending it here makes the parser drop it, rather than
// glue it to the next Mac's hello.
static void rx_end_line(void)
{
    if (s_rx_last != '\n') {
        const uint8_t nl = '\n';
        rx_put(&nl, 1);
    }
}

int hal_link_read(uint8_t *out, int max)
{
    if (!s_rx_ring || max <= 0) return 0;
    const uint32_t head = __atomic_load_n(&s_rx_head, __ATOMIC_ACQUIRE);
    uint32_t tail = s_rx_tail;
    int n = 0;
    while (n < max && tail != head) out[n++] = s_rx_ring[tail++ % RX_RING_BYTES];
    __atomic_store_n(&s_rx_tail, tail, __ATOMIC_RELEASE);
    return n;
}

// ---- GATT ---------------------------------------------------------------------------------------------------

static bool link_is_secure(uint16_t conn)
{
    struct ble_gap_conn_desc d;
    return ble_gap_conn_find(conn, &d) == 0 && d.sec_state.encrypted && d.sec_state.authenticated;
}

static int chr_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) return 0; // TX reads as empty
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    // The flags already require a paired, encrypted link. Check again before anything reaches the parser.
    if (!link_is_secure(conn)) return BLE_ATT_ERR_INSUFFICIENT_AUTHEN;
    uint8_t buf[512];
    uint16_t len = 0;
    if (ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof buf, &len) != 0) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    if (!rx_put(buf, len)) {
        ESP_LOGW(TAG, "the receive ring is full: %u bytes lost", len);
        return BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    return 0;
}

static const struct ble_gatt_svc_def SERVICES[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &RX_UUID.u,
                .access_cb = chr_access,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP | BLE_GATT_CHR_F_WRITE_ENC | BLE_GATT_CHR_F_WRITE_AUTHEN,
            },
            {
                .uuid = &TX_UUID.u,
                .access_cb = chr_access,
                .val_handle = &s_tx_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_READ_ENC | BLE_GATT_CHR_F_READ_AUTHEN,
            },
            {0},
        },
    },
    {0},
};

// ---- GAP ----------------------------------------------------------------------------------------------------

static void advertise(void);

static int gap_event(struct ble_gap_event *ev, void *arg)
{
    switch (ev->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (ev->connect.status != 0) {
            advertise();
            return 0;
        }
        s_conn = ev->connect.conn_handle;
        ESP_LOGI(TAG, "a Mac connected; asking for encryption");
        esp_timer_stop(s_secure_timer);
        esp_timer_start_once(s_secure_timer, (uint64_t)(pair_mode() ? SECURE_PAIRING_S : SECURE_WITHIN_S) * 1000000);
        ble_gap_security_initiate(s_conn); // a paired Mac encrypts; a new one pairs (the screen shows a code)
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "disconnected (reason 0x%x)", ev->disconnect.reason);
        esp_timer_stop(s_secure_timer);
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        s_secure = s_subscribed = s_pairing = s_new_pair = false;
        s_code = 0;
        peer_set("");
        rx_end_line();
        advertise();
        return 0;
    case BLE_GAP_EVENT_ENC_CHANGE: {
        s_pairing = false;
        s_code = 0;
        struct ble_gap_conn_desc d;
        if (ev->enc_change.status == 0 && ble_gap_conn_find(ev->enc_change.conn_handle, &d) == 0) {
            s_secure = d.sec_state.encrypted && d.sec_state.authenticated;
            char key[16];
            addr_key(&d.peer_id_addr, key);
            peer_set(key);
            ESP_LOGI(TAG, "encrypted (paired: %d, authenticated: %d, new: %d)", d.sec_state.bonded, d.sec_state.authenticated, s_new_pair);
            if (s_secure) {
                esp_timer_stop(s_secure_timer);
                if (s_new_pair) s_pair_until = 0; // the new Mac paired: pair mode is done
            } else {
                // Encrypted with no passkey (a Mac with no keyboard): it must not take one of the four places.
                ble_store_util_delete_peer(&d.peer_id_addr);
                ble_gap_terminate(ev->enc_change.conn_handle, BLE_ERR_AUTH_FAIL);
            }
        } else if (s_secure && ble_gap_conn_find(ev->enc_change.conn_handle, &d) == 0 && d.sec_state.encrypted && d.sec_state.authenticated) {
            // A paired Mac often encrypts with its stored key before this board's own request (at connect) is answered.
            // That request then times out 30 s later, on a link that's already encrypted and authenticated: keep it.
            ESP_LOGI(TAG, "a late security request ended (status %d) on an encrypted link: kept", ev->enc_change.status);
        } else {
            ESP_LOGW(TAG, "encryption failed (status %d)", ev->enc_change.status);
            ble_gap_terminate(ev->enc_change.conn_handle, BLE_ERR_AUTH_FAIL);
        }
        return 0;
    }
    case BLE_GAP_EVENT_PASSKEY_ACTION:
        if (ev->passkey.params.action == BLE_SM_IOACT_DISP) {
            if (!pair_mode()) { // an unknown Mac: only in pair mode (Settings › Pair a new Mac)
                ESP_LOGW(TAG, "a new Mac tried to pair outside pair mode: refused");
                ble_gap_terminate(ev->passkey.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
                return 0;
            }
            s_new_pair = true;
            struct ble_sm_io io = {.action = BLE_SM_IOACT_DISP, .passkey = esp_random() % 1000000};
            s_code = io.passkey;
            s_pairing = true;
            ESP_LOGI(TAG, "pairing: showing the code"); // (the code itself is never logged)
            ble_sm_inject_io(ev->passkey.conn_handle, &io);
        }
        return 0;
    case BLE_GAP_EVENT_REPEAT_PAIRING: {
        // The peer lost its keys (for example after "Forget This Device"), or says it did. Only in pair mode do ours make way
        // for new ones, so nobody who merely borrows a paired Mac's address can erase its keys.
        if (!pair_mode()) {
            ESP_LOGW(TAG, "a paired Mac asked to pair again outside pair mode: refused");
            return BLE_GAP_REPEAT_PAIRING_IGNORE;
        }
        struct ble_gap_conn_desc d;
        if (ble_gap_conn_find(ev->repeat_pairing.conn_handle, &d) == 0) ble_store_util_delete_peer(&d.peer_id_addr);
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    }
    case BLE_GAP_EVENT_SUBSCRIBE:
        if (ev->subscribe.attr_handle == s_tx_handle) s_subscribed = ev->subscribe.cur_notify;
        return 0;
    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "MTU %d", ev->mtu.value);
        return 0;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        if (s_conn == BLE_HS_CONN_HANDLE_NONE) advertise();
        return 0;
    }
    return 0;
}

static void advertise(void)
{
    // The service UUID in the advertisement (the desktop app looks for it), the name in the scan response.
    struct ble_hs_adv_fields f = {0};
    f.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    f.uuids128 = &SVC_UUID;
    f.num_uuids128 = 1;
    f.uuids128_is_complete = 1;
    int rc = ble_gap_adv_set_fields(&f);
    struct ble_hs_adv_fields r = {0};
    r.name = (const uint8_t *)BLE_NAME;
    r.name_len = strlen(BLE_NAME);
    r.name_is_complete = 1;
    if (rc == 0) rc = ble_gap_adv_rsp_set_fields(&r);
    const struct ble_gap_adv_params p = {.conn_mode = BLE_GAP_CONN_MODE_UND, .disc_mode = BLE_GAP_DISC_MODE_GEN};
    if (rc == 0) rc = ble_gap_adv_start(s_addr_type, NULL, BLE_HS_FOREVER, &p, gap_event, NULL);
    if (rc != 0 && rc != BLE_HS_EALREADY) ESP_LOGW(TAG, "can't advertise (%d)", rc);
    else ESP_LOGI(TAG, "advertising as \"%s\"", BLE_NAME);
}

// ---- Which Mac may use the board ----------------------------------------------------------------------------

const char *hal_link_refuse_reason(void)
{
    if (s_new_pair) return NULL; // the Mac that just paired stays
    if (pair_mode()) return "pairing";
    char peer[16];
    peer_get(peer);
    if (s_chosen[0] && strcmp(s_chosen, peer)) return "another Mac is chosen";
    return NULL;
}

static void kick_now(void *arg)
{
    const uint16_t conn = s_conn;
    if (conn != BLE_HS_CONN_HANDLE_NONE) ble_gap_terminate(conn, BLE_ERR_REM_USER_CONN_TERM);
}

void hal_link_kick(const char *reason)
{
    char line[96];
    const int n = snprintf(line, sizeof line, "{\"t\":\"busy\",\"reason\":\"%s\"}\n", reason);
    hal_link_send(line, (size_t)n);
    ESP_LOGI(TAG, "sending the Mac away: %s", reason);
    esp_timer_stop(s_kick_timer);
    esp_timer_start_once(s_kick_timer, KICK_DELAY_MS * 1000);
}

// The names of Macs that are no longer paired: NimBLE drops the oldest pairing by itself when a fifth Mac pairs.
static void forget_unpaired_names(nvs_handle_t h)
{
    ble_addr_t addrs[HAL_MACS_MAX];
    int n = 0;
    if (ble_store_util_bonded_peers(addrs, &n, HAL_MACS_MAX) != 0) return;
    char stale[8][16];
    int ns = 0;
    nvs_iterator_t it = NULL;
    for (esp_err_t e = nvs_entry_find(NVS_DEFAULT_PART_NAME, NVS_MACS, NVS_TYPE_STR, &it); e == ESP_OK && ns < 8; e = nvs_entry_next(&it)) {
        nvs_entry_info_t info;
        nvs_entry_info(it, &info);
        if (!strcmp(info.key, NVS_CHOSEN)) continue;
        bool paired = false;
        for (int i = 0; i < n; i++) {
            char k[16];
            addr_key(&addrs[i], k);
            if (!strcmp(k, info.key)) paired = true;
        }
        if (!paired) strlcpy(stale[ns++], info.key, sizeof stale[0]);
    }
    nvs_release_iterator(it);
    for (int i = 0; i < ns; i++) nvs_erase_key(h, stale[i]);
}

void hal_link_remember_host(const char *name)
{
    char peer[16];
    peer_get(peer);
    if (!peer[0] || !name[0]) return;
    // At most what Settings shows, never cut inside a UTF-8 character.
    char kept[sizeof ((hal_mac_t *)0)->name];
    size_t len = strlen(name);
    if (len >= sizeof kept) {
        len = sizeof kept - 1;
        while (len > 0 && ((uint8_t)name[len] & 0xC0) == 0x80) len--;
    }
    memcpy(kept, name, len);
    kept[len] = 0;
    nvs_handle_t h;
    if (nvs_open(NVS_MACS, NVS_READWRITE, &h) == ESP_OK) {
        forget_unpaired_names(h);
        nvs_set_str(h, peer, kept);
        nvs_commit(h);
        nvs_close(h);
    }
}

// ---- Sending ------------------------------------------------------------------------------------------------

#define NOTIFY_TRIES 20     // when the host's buffers are full, wait this many times ...
#define NOTIFY_WAIT_MS 10   // ... this long: a voice upload sends faster than the link drains

static bool listening(void) { return s_conn != BLE_HS_CONN_HANDLE_NONE && s_secure && s_subscribed; }

static bool notify(uint16_t conn, const char *data, size_t n)
{
    for (int tries = 0; tries < NOTIFY_TRIES && listening(); tries++) {
        struct os_mbuf *om = ble_hs_mbuf_from_flat(data, (uint16_t)n);
        if (om) {
            const int rc = ble_gatts_notify_custom(conn, s_tx_handle, om); // it takes om, whatever the outcome
            if (rc == 0) return true;
            if (rc != BLE_HS_ENOMEM && rc != BLE_HS_EBUSY) return false;
        }
        vTaskDelay(pdMS_TO_TICKS(NOTIFY_WAIT_MS));
    }
    return false;
}

bool hal_link_send(const char *data, size_t len)
{
    const uint16_t conn = s_conn;
    if (!listening() || !s_send_lock) return false;
    xSemaphoreTake(s_send_lock, portMAX_DELAY);
    bool ok = true;
    if (s_tx_broken) { // end the half line the last failure left, so the Mac's parser drops it, and not this one too
        ok = notify(conn, "\n", 1);
        s_tx_broken = !ok;
    }
    const size_t mtu = ble_att_mtu(conn);
    const size_t chunk = mtu > 3 ? mtu - 3 : 20;
    for (size_t off = 0; ok && off < len; off += chunk) {
        ok = notify(conn, data + off, len - off < chunk ? len - off : chunk);
        if (!ok && off > 0) s_tx_broken = true;
    }
    xSemaphoreGive(s_send_lock);
    return ok;
}

void hal_link_status(hal_link_status_t *out)
{
    const bool connected = s_conn != BLE_HS_CONN_HANDLE_NONE;
    out->connected = connected && s_secure;
    out->pairing = connected && s_pairing;
    out->code = s_code;
}

// ---- The Settings screen's view -----------------------------------------------------------------------------

static void chosen_save(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_MACS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_str(h, NVS_CHOSEN, s_chosen);
        nvs_commit(h);
        nvs_close(h);
    }
}

int hal_macs_list(hal_mac_t *out, int max)
{
    ble_addr_t addrs[HAL_MACS_MAX];
    int n = 0;
    if (ble_store_util_bonded_peers(addrs, &n, HAL_MACS_MAX) != 0) return 0;
    nvs_handle_t h;
    const bool have_names = nvs_open(NVS_MACS, NVS_READONLY, &h) == ESP_OK;
    char peer[16];
    peer_get(peer);
    int k = 0;
    for (int i = 0; i < n && k < max; i++, k++) {
        addr_key(&addrs[i], out[k].key);
        size_t len = sizeof out[k].name;
        if (!have_names || nvs_get_str(h, out[k].key, out[k].name, &len) != ESP_OK) strlcpy(out[k].name, "A Mac", sizeof out[k].name);
        out[k].connected = s_secure && !strcmp(out[k].key, peer);
    }
    if (have_names) nvs_close(h);
    return k;
}

void hal_macs_chosen(char *out, size_t n) { strlcpy(out, s_chosen, n); }

void hal_macs_choose(const char *key)
{
    strlcpy(s_chosen, key, sizeof s_chosen);
    chosen_save();
    char peer[16];
    peer_get(peer);
    if (key[0] && s_secure && strcmp(key, peer)) hal_link_kick("another Mac is chosen");
}

void hal_macs_forget(const char *key)
{
    ble_addr_t addrs[HAL_MACS_MAX];
    int n = 0;
    ble_store_util_bonded_peers(addrs, &n, HAL_MACS_MAX);
    for (int i = 0; i < n; i++) {
        char k[16];
        addr_key(&addrs[i], k);
        if (strcmp(k, key)) continue;
        char peer[16];
        peer_get(peer);
        if (s_secure && !strcmp(k, peer)) kick_now(NULL);
        ble_store_util_delete_peer(&addrs[i]);
        nvs_handle_t h;
        if (nvs_open(NVS_MACS, NVS_READWRITE, &h) == ESP_OK) {
            nvs_erase_key(h, k);
            nvs_commit(h);
            nvs_close(h);
        }
        ESP_LOGI(TAG, "forgot a Mac");
    }
    if (!strcmp(s_chosen, key)) {
        s_chosen[0] = 0;
        chosen_save();
    }
}

void hal_macs_pair_start(void)
{
    s_pair_until = esp_timer_get_time() + (int64_t)HAL_PAIR_SECONDS * 1000000;
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) hal_link_kick("pairing"); // make room: one connection at a time
    ESP_LOGI(TAG, "pair mode for %d s", HAL_PAIR_SECONDS);
}

void hal_macs_pair_cancel(void) { s_pair_until = 0; }

int hal_macs_pair_left(void)
{
    const int64_t left = s_pair_until - esp_timer_get_time();
    return left > 0 ? (int)((left + 999999) / 1000000) : 0;
}

// ---- Start --------------------------------------------------------------------------------------------------

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &s_addr_type);
    advertise();
}

static void on_reset(int reason) { ESP_LOGW(TAG, "host reset (%d)", reason); }

static void host_task(void *arg)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

void hal_link_init(void)
{
    s_rx_ring = hal_alloc_large(RX_RING_BYTES);
    s_send_lock = xSemaphoreCreateMutex();
    nvs_handle_t h;
    if (nvs_open(NVS_MACS, NVS_READONLY, &h) == ESP_OK) {
        size_t n = sizeof s_chosen;
        if (nvs_get_str(h, NVS_CHOSEN, s_chosen, &n) != ESP_OK) s_chosen[0] = 0;
        nvs_close(h);
    }
    if (nimble_port_init() != ESP_OK) {
        ESP_LOGE(TAG, "Bluetooth didn't start");
        return;
    }
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    // Pairing: the screen shows a code and the Mac types it (MITM protection), LE Secure Connections, bonding. Legacy
    // pairing isn't built in (sdkconfig.defaults), so a peer that doesn't offer Secure Connections fails here.
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_DISP_ONLY;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 1;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ESP_ERROR_CHECK(ble_gatts_count_cfg(SERVICES));
    ESP_ERROR_CHECK(ble_gatts_add_svcs(SERVICES));
    ble_svc_gap_device_name_set(BLE_NAME);
    ble_store_config_init();
    const esp_timer_create_args_t kick = {.callback = kick_now, .name = "ble_kick"};
    ESP_ERROR_CHECK(esp_timer_create(&kick, &s_kick_timer));
    ESP_ERROR_CHECK(esp_timer_create(&kick, &s_secure_timer)); // the same action: drop the link
    nimble_port_freertos_init(host_task);
}
