// Key/value storage (NVS) and PBKDF2.
#include <string.h>

#include "esp_log.h"
#include "mbedtls/md.h"
#include "mbedtls/pkcs5.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "board_hw.h"
#include "hal.h"

static const char *TAG = "hal_store";

static nvs_handle_t s_nvs;
static bool s_ok;

void hal_store_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "formatting the store");
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    s_ok = err == ESP_OK && nvs_open("bento", NVS_READWRITE, &s_nvs) == ESP_OK;
    if (!s_ok) ESP_LOGE(TAG, "no store: %s", esp_err_to_name(err));
}

bool hal_store_get(const char *key, void *out, size_t *len) { return s_ok && nvs_get_blob(s_nvs, key, out, len) == ESP_OK; }

bool hal_store_set(const char *key, const void *data, size_t len)
{
    return s_ok && nvs_set_blob(s_nvs, key, data, len) == ESP_OK && nvs_commit(s_nvs) == ESP_OK;
}

void hal_store_erase(const char *key)
{
    if (s_ok && nvs_erase_key(s_nvs, key) == ESP_OK) nvs_commit(s_nvs);
}

void hal_pbkdf2_sha256(const char *password, const uint8_t *salt, size_t salt_len, uint32_t iterations, uint8_t out[32])
{
    if (mbedtls_pkcs5_pbkdf2_hmac_ext(MBEDTLS_MD_SHA256, (const unsigned char *)password, strlen(password), salt, salt_len, iterations, 32, out) != 0) {
        // All ones, not leftover bytes, so a failure can't match a stored hash by chance.
        memset(out, 0xFF, 32);
    }
}
