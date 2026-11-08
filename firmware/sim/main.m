// bento-sim: a native Mac window showing one board at its size, in its real shape. Mouse = touch; scroll wheel and arrow
// keys = the knob. It runs the real UX on its own thread, through hal_sim.c. --headless has no window, for tests.
#import <AppKit/AppKit.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sim.h"

static int s_at_x = -1, s_at_y = -1; // --at
static bool s_scale_fixed = false;   // --scale

@interface SimView : NSView
@property(nonatomic) float shownTilt;
@end

@implementation SimView {
    uint16_t *_rgb565;
    uint32_t *_rgba;
    CGColorSpaceRef _space;
    uint64_t _gen;
    double _scrollAcc;
    NSTextField *_label;
    int _dragging;
    int _shownAgents;
    NSSize _drawnSize;
}

- (instancetype)initWithFrame:(NSRect)frame
{
    self = [super initWithFrame:frame];
    const size_t n = SIM.view ? 960 * 960 : (size_t)BOARD.width * (size_t)BOARD.height; // a viewer can show any board
    _rgb565 = calloc(n, 2);
    _rgba = calloc(n, 4);
    _space = CGColorSpaceCreateDeviceRGB();
    return self;
}

- (BOOL)acceptsFirstResponder { return YES; }

- (void)setFrameSize:(NSSize)size
{
    [super setFrameSize:size];
    [self setNeedsDisplay:YES]; // the board stays centred, at a scale that fits the new size
}
- (BOOL)isFlipped { return YES; }

// Whole pixels, so the panel's pixels land exactly on the screen's.
- (NSPoint)panelCenter
{
    const NSRect b = self.bounds;
    return NSMakePoint(floor(b.size.width / 2), floor((b.size.height - 56) / 2));
}

// How many window points per panel pixel: --scale, or as many whole screen pixels as fit the window, which land exactly
// on the screen's. In a window too small for one, the panel shrinks smoothly, and that stays sharp too.
- (float)panelScale
{
    if (s_scale_fixed) return (float)SIM.scale;
    const NSRect b = self.bounds;
    const CGFloat backing = self.window ? self.window.backingScaleFactor : 1;
    const CGFloat room = (MIN(b.size.width, b.size.height - 56) - 56) * backing;
    const CGFloat fit = MAX(room / MAX(BOARD.width, BOARD.height), 0.1);
    return (float)((fit >= 1 ? floor(fit) : fit) / backing);
}

- (void)tick
{
    if (SIM.view) sim_view_sync();
    const float target = sim_tilt();
    float d = target - _shownTilt;
    while (d > 180) d -= 360;
    while (d < -180) d += 360;
    const float next = fabsf(d) < 0.5f ? target : _shownTilt + d * 0.25f;
    const bool moved = next != _shownTilt;
    _shownTilt = next;
    // A new frame, a turn, or a new size: a tiling window manager resizes the window after it opens, and a board that's
    // showing a still screen sends no new frame, so a resize alone has to redraw it too.
    const uint64_t gen = sim_panel_generation();
    const bool resized = !NSEqualSizes(self.bounds.size, _drawnSize);
    if (gen != _gen || moved || resized) {
        _gen = gen;
        [self setNeedsDisplay:YES];
    }
    char text[48];
    sim_feedback_text(text, sizeof text);
    if (self.label) self.label.stringValue = [NSString stringWithUTF8String:text];
    if (SIM.demo && sim_agent_count() != _shownAgents) {
        _shownAgents = sim_agent_count();
        self.window.title = [NSString stringWithFormat:@"Bento: %s, %d agents", BOARD.name, _shownAgents];
    }
}

- (NSTextField *)label { return _label; }
- (void)setLabel:(NSTextField *)l { _label = l; }

- (void)drawRect:(NSRect)dirty
{
    (void)dirty;
    CGContextRef c = [[NSGraphicsContext currentContext] CGContext];
    const NSRect b = self.bounds;
    _drawnSize = b.size;
    CGContextSetRGBFillColor(c, 0.09, 0.09, 0.085, 1);
    CGContextFillRect(c, b);
    const int W = BOARD.width, H = BOARD.height;
    sim_copy_panel(_rgb565);
    for (int i = 0; i < W * H; i++) {
        const uint32_t p = _rgb565[i];
        const uint32_t r = ((p >> 8) & 0xF8) | (p >> 13), g = ((p >> 3) & 0xFC) | ((p >> 9) & 3), bl = ((p << 3) & 0xF8) | ((p >> 2) & 7);
        _rgba[i] = r | g << 8 | bl << 16 | 0xFF000000u;
    }
    CGDataProviderRef dp = CGDataProviderCreateWithData(NULL, _rgba, (size_t)W * (size_t)H * 4, NULL);
    CGImageRef img = CGImageCreate((size_t)W, (size_t)H, 8, 32, (size_t)W * 4, _space, kCGImageAlphaNoneSkipLast | kCGBitmapByteOrderDefault, dp, NULL, false, kCGRenderingIntentDefault);
    const NSPoint mid = [self panelCenter];
    const float s = [self panelScale];
    CGContextSaveGState(c);
    CGContextTranslateCTM(c, mid.x, mid.y);
    CGContextRotateCTM(c, (_shownTilt + (float)BOARD.mount_deg) * 3.14159265f / 180.0f);
    const CGRect r = CGRectMake(-W * s / 2, -H * s / 2, W * s, H * s);
    // The board around the screen.
    if (BOARD.shape == SHAPE_ROUND) {
        CGContextSetRGBFillColor(c, 0.2, 0.2, 0.22, 1);
        const CGFloat body = 22 * SIM_RENDER * s, bezel = 3 * SIM_RENDER * s;
        CGContextFillEllipseInRect(c, CGRectInset(r, -body, -body));
        CGContextSetRGBFillColor(c, 0, 0, 0, 1);
        CGContextFillEllipseInRect(c, CGRectInset(r, -bezel, -bezel));
        CGContextAddEllipseInRect(c, r);
    } else {
        const CGFloat frame_r = W * s * 0.12f;
        CGPathRef outer = CGPathCreateWithRoundedRect(CGRectInset(r, -W * s * 0.04f, -W * s * 0.04f), frame_r, frame_r, NULL);
        CGContextSetRGBFillColor(c, 0.04, 0.04, 0.05, 1);
        CGContextAddPath(c, outer);
        CGContextFillPath(c);
        CGPathRelease(outer);
        const CGFloat corner = W * s * 0.085f;
        CGPathRef path = CGPathCreateWithRoundedRect(r, corner, corner, NULL);
        CGContextAddPath(c, path);
        CGPathRelease(path);
    }
    CGContextClip(c);
    const CGFloat on_screen = s * self.window.backingScaleFactor;
    CGContextSetInterpolationQuality(c, _shownTilt == 0 && on_screen == floor(on_screen) ? kCGInterpolationNone : kCGInterpolationHigh);
    CGContextScaleCTM(c, 1, -1); // images draw bottom-up in a flipped view
    CGContextDrawImage(c, CGRectMake(r.origin.x, -r.origin.y - r.size.height, r.size.width, r.size.height), img);
    CGContextScaleCTM(c, 1, -1);
    static const float LIT[HAL_BRIGHT_LEVELS] = {0.06f, 0.1f, 0.15f, 0.22f, 0.3f, 0.4f, 0.52f, 0.66f, 0.82f, 1}; // as it looks, not as it's driven
    const int level = sim_brightness();
    const float dim = 1 - LIT[level < 0 ? 0 : level >= HAL_BRIGHT_LEVELS ? HAL_BRIGHT_LEVELS - 1 : level];
    if (dim > 0) {
        CGContextSetRGBFillColor(c, 0, 0, 0, dim);
        CGContextFillRect(c, r);
    }
    CGContextRestoreGState(c);
    CGImageRelease(img);
    CGDataProviderRelease(dp);
}

// The mouse, on the panel: the window's tilt undone.
- (void)panelPoint:(NSEvent *)e x:(int *)x y:(int *)y
{
    const NSPoint p = [self convertPoint:e.locationInWindow fromView:nil];
    const NSPoint mid = [self panelCenter];
    const float s = [self panelScale], dx = (float)(p.x - mid.x) / s, dy = (float)(p.y - mid.y) / s;
    const float a = -(_shownTilt + (float)BOARD.mount_deg) * 3.14159265f / 180.0f;
    const float rx = dx * cosf(a) - dy * sinf(a), ry = dx * sinf(a) + dy * cosf(a);
    *x = (int)(rx + (float)BOARD.width / 2);
    *y = (int)(ry + (float)BOARD.height / 2);
}

- (void)mouseDown:(NSEvent *)e
{
    if (SIM.view) return; // only something to watch: a test is driving the board
    int x, y;
    [self panelPoint:e x:&x y:&y];
    if (x < 0 || y < 0 || x >= BOARD.width || y >= BOARD.height) return;
    _dragging = 1;
    sim_touch(HW_TOUCH_DOWN, x, y);
}

- (void)mouseDragged:(NSEvent *)e
{
    if (!_dragging) return;
    int x, y;
    [self panelPoint:e x:&x y:&y];
    sim_touch(HW_TOUCH_MOVE, x, y);
}

- (void)mouseUp:(NSEvent *)e
{
    if (!_dragging) return;
    _dragging = 0;
    int x, y;
    [self panelPoint:e x:&x y:&y];
    sim_touch(HW_TOUCH_UP, x, y);
}

- (void)scrollWheel:(NSEvent *)e
{
    if (SIM.view || !BOARD.has.knob) return;
    _scrollAcc += e.scrollingDeltaY;
    const double step = e.hasPreciseScrollingDeltas ? 12 : 1;
    while (_scrollAcc >= step) {
        sim_knob(1);
        _scrollAcc -= step;
    }
    while (_scrollAcc <= -step) {
        sim_knob(-1);
        _scrollAcc += step;
    }
}

- (void)keyDown:(NSEvent *)e
{
    if (SIM.view) return;
    NSString *ch = e.charactersIgnoringModifiers;
    if (!ch.length) return;
    const unichar k = [ch characterAtIndex:0];
    switch (k) {
    case NSRightArrowFunctionKey:
    case NSUpArrowFunctionKey:
        sim_knob(1);
        return;
    case NSLeftArrowFunctionKey:
    case NSDownArrowFunctionKey:
        sim_knob(-1);
        return;
    case 'm': sim_button(BUTTON_MENU); return;
    case 'u': sim_button(BUTTON_UP); return;
    case 'd': sim_button(BUTTON_DOWN); return;
    case 't': if (BOARD.has.motion) sim_set_tilt((float)(((int)sim_tilt() / 90 + 1) % 4 * 90)); return;
    case 'f': if (BOARD.has.motion) sim_set_flat(!sim_flat()); return;
    case 'o': sim_toggle_mac(); return;
    case 'n': sim_toggle_needs(); return;
    case 'a': sim_add_agents(1); return;
    case 'x': sim_add_agents(-1); return;
    default: break;
    }
}
@end

@interface SimController : NSObject <NSApplicationDelegate, NSWindowDelegate>
@property(strong) NSWindow *window;
@property(strong) SimView *view;
@end

@implementation SimController
- (NSButton *)button:(NSString *)title action:(SEL)sel at:(CGFloat)x y:(CGFloat)y width:(CGFloat)w
{
    NSButton *b = [NSButton buttonWithTitle:title target:self action:sel];
    b.bezelStyle = NSBezelStyleRounded;
    b.controlSize = NSControlSizeSmall;
    b.font = [NSFont systemFontOfSize:11];
    b.frame = NSMakeRect(x, y, w, 24);
    b.refusesFirstResponder = YES;
    b.autoresizingMask = NSViewMinYMargin; // stays at the bottom when the window resizes
    [self.view addSubview:b];
    return b;
}

- (void)menuPressed { sim_button(BUTTON_MENU); }
- (void)upPressed { sim_button(BUTTON_UP); }
- (void)downPressed { sim_button(BUTTON_DOWN); }
- (void)tiltPressed { sim_set_tilt((float)(((int)sim_tilt() / 90 + 1) % 4 * 90)); }
- (void)flatPressed { sim_set_flat(!sim_flat()); }
- (void)macPressed { sim_toggle_mac(); }
- (void)needsPressed { sim_toggle_needs(); }
- (void)addAgentPressed { sim_add_agents(1); }
- (void)removeAgentPressed { sim_add_agents(-1); }

- (void)applicationDidFinishLaunching:(NSNotification *)n
{
    (void)n;
    const int W = BOARD.width * SIM.scale, H = BOARD.height * SIM.scale;
    const int pad = BOARD.has.motion ? (int)((float)W * 0.36f) : 48;
    const NSSize room = NSScreen.mainScreen.visibleFrame.size;
    const NSRect frame = NSMakeRect(0, 0, MIN(W + pad * 2, room.width), MIN(H + pad * 2 + 56, room.height - 32));
    self.window = [[NSWindow alloc] initWithContentRect:frame styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    self.window.title = SIM.view ? @"Bento: tests" : [NSString stringWithFormat:@"Bento: %s", BOARD.name];
    self.window.delegate = self;
    // An ordinary resizable window, so a tiling window manager takes it. The board stays centered at its real size.
    self.window.collectionBehavior = NSWindowCollectionBehaviorFullScreenPrimary;
    self.window.minSize = NSMakeSize(240, 240);
    self.view = [[SimView alloc] initWithFrame:frame];
    self.window.contentView = self.view;

    CGFloat x = 12;
    const CGFloat y = frame.size.height - 32;
    if (!SIM.view && !BOARD.has.knob) {
        [self button:@"Menu (M)" action:@selector(menuPressed) at:x y:y width:76]; x += 80;
        [self button:@"Up (U)" action:@selector(upPressed) at:x y:y width:62]; x += 66;
        [self button:@"Down (D)" action:@selector(downPressed) at:x y:y width:72]; x += 76;
    }
    if (!SIM.view && BOARD.has.motion) {
        [self button:@"Tilt 90° (T)" action:@selector(tiltPressed) at:x y:y width:82]; x += 86;
        [self button:@"Flat (F)" action:@selector(flatPressed) at:x y:y width:62]; x += 66;
    }
    if (SIM.demo) {
        [self button:@"Mac (O)" action:@selector(macPressed) at:x y:y width:64]; x += 68;
        [self button:@"Needs you (N)" action:@selector(needsPressed) at:x y:y width:96]; x += 100;
        [self button:@"+ Agent (A)" action:@selector(addAgentPressed) at:x y:y width:82]; x += 86;
        [self button:@"\u2212 Agent (X)" action:@selector(removeAgentPressed) at:x y:y width:82]; x += 86;
    }
    NSTextField *label = [NSTextField labelWithString:@""];
    label.font = [NSFont systemFontOfSize:12 weight:NSFontWeightSemibold];
    label.textColor = [NSColor colorWithRed:0.9 green:0.7 blue:0.35 alpha:1];
    label.alignment = NSTextAlignmentCenter;
    label.frame = NSMakeRect(0, frame.size.height - 56, frame.size.width, 20);
    label.autoresizingMask = NSViewMinYMargin | NSViewWidthSizable;
    [self.view addSubview:label];
    [self.view setLabel:label];

    if (s_at_x >= 0) {
        // --at X,Y: the window's top-left corner, measured from the screen's top-left.
        const NSRect screen = NSScreen.mainScreen.frame;
        [self.window setFrameTopLeftPoint:NSMakePoint(screen.origin.x + s_at_x, NSMaxY(screen) - s_at_y)];
    } else {
        [self.window center];
    }
    [self.window makeKeyAndOrderFront:nil];
    [self.window makeFirstResponder:self.view];
    [NSApp activateIgnoringOtherApps:YES];
    [NSTimer scheduledTimerWithTimeInterval:1.0 / 60 repeats:YES block:^(NSTimer *t) {
        (void)t;
        [self.view tick];
    }];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)a
{
    (void)a;
    return YES;
}
@end

// What the window does for "%window", "%click", "%key" and "%scroll": on its own thread, through its real handlers.
static SimController *s_controller;

static NSEvent *mouse_event(NSEventType type, NSPoint at, NSWindow *w)
{
    return [NSEvent mouseEventWithType:type location:at modifierFlags:0 timestamp:0 windowNumber:w.windowNumber context:nil eventNumber:0 clickCount:1 pressure:1];
}

static void window_command_on_main(const char *cmd)
{
    SimView *v = s_controller.view;
    NSWindow *w = s_controller.window;
    if (!strncmp(cmd, "window ", 7)) {
        // The view as drawn, including its controls, with no need for permission to record the screen.
        [v tick];
        NSBitmapImageRep *rep = [v bitmapImageRepForCachingDisplayInRect:v.bounds];
        [v cacheDisplayInRect:v.bounds toBitmapImageRep:rep];
        NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        [png writeToFile:[NSString stringWithUTF8String:cmd + 7] atomically:YES];
    } else if (!strncmp(cmd, "click ", 6)) {
        // A panel pixel, wherever the tilt shows it: the view's position, then a press and a release there.
        int x = 0, y = 0;
        sscanf(cmd + 6, "%d %d", &x, &y);
        const float a = (v.shownTilt + (float)BOARD.mount_deg) * 3.14159265f / 180.0f;
        const float scale = [v panelScale], dx = ((float)x - (float)BOARD.width / 2) * scale, dy = ((float)y - (float)BOARD.height / 2) * scale;
        const NSPoint mid = [v panelCenter];
        const NSPoint in_view = NSMakePoint(mid.x + dx * cosf(a) - dy * sinf(a), mid.y + dx * sinf(a) + dy * cosf(a));
        const NSPoint in_window = [v convertPoint:in_view toView:nil];
        [v mouseDown:mouse_event(NSEventTypeLeftMouseDown, in_window, w)];
        [v mouseUp:mouse_event(NSEventTypeLeftMouseUp, in_window, w)];
    } else if (!strncmp(cmd, "key ", 4)) {
        unichar k = (unichar)cmd[4];
        if (!strcmp(cmd + 4, "right")) k = NSRightArrowFunctionKey;
        if (!strcmp(cmd + 4, "left")) k = NSLeftArrowFunctionKey;
        NSString *ch = [NSString stringWithCharacters:&k length:1];
        [v keyDown:[NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:0 timestamp:0 windowNumber:w.windowNumber context:nil characters:ch charactersIgnoringModifiers:ch isARepeat:NO keyCode:0]];
    } else if (!strncmp(cmd, "scroll ", 7)) {
        CGEventRef ce = CGEventCreateScrollWheelEvent(NULL, kCGScrollEventUnitLine, 1, atoi(cmd + 7));
        [v scrollWheel:[NSEvent eventWithCGEvent:ce]];
        CFRelease(ce);
    }
}

static void window_command(const char *cmd)
{
    NSString *copy = [NSString stringWithUTF8String:cmd];
    dispatch_sync(dispatch_get_main_queue(), ^{
        window_command_on_main(copy.UTF8String);
    });
}

static void *ux_thread(void *arg)
{
    (void)arg;
    sim_run_ux();
    return NULL;
}

static void usage(void)
{
    fprintf(stderr,
            "bento-sim --board knob-lcd-1.8|amoled-2.16 [options]\n"
            "  --headless        no window, for tests\n"
            "  --at X,Y          where the window's top-left corner goes\n"
            "  --demo            a stand-in for the desktop app, with sample agents\n"
            "  --agents N        with --demo: start with N agents, up to 512 (default 5)\n"
            "  --wav FILE        the mic plays this 16 kHz mono WAV file, then silence\n"
            "  --store FILE      where to keep storage (default: firmware/sim/build/store-<board>.bin)\n"
            "  --link-port N     default 7777\n"
            "  --debug-port N    default 7778\n"
            "  --scale N         window pixels per panel pixel (default: as many as fit)\n"
            "  --panel-file F    also keep the panel in file F, for a viewer\n"
            "  --view F          no board: one window that shows the panel in file F, through restarts\n"
            "In the window: mouse = touch; scroll wheel or arrow keys = the knob; M, U, D = the Menu, Up and Down buttons;\n"
            "T = tilt 90 degrees; F = flat; with --demo, O = Mac on or off, N = an agent needs you, A and X = one more or one fewer agent.\n");
}

int main(int argc, char **argv)
{
    SIM.link_port = 7777;
    SIM.debug_port = 7778;
    SIM.scale = 1;
    SIM.argc = argc;
    SIM.argv = argv;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--board") && i + 1 < argc) SIM.board = argv[++i];
        else if (!strcmp(a, "--at") && i + 1 < argc) sscanf(argv[++i], "%d,%d", &s_at_x, &s_at_y);
        else if (!strcmp(a, "--headless")) SIM.headless = true;
        else if (!strcmp(a, "--panel-file") && i + 1 < argc) SIM.panel_file = argv[++i];
        else if (!strcmp(a, "--view") && i + 1 < argc) { SIM.view = argv[++i]; SIM.board = "knob-lcd-1.8"; }
        else if (!strcmp(a, "--demo")) SIM.demo = true;
        else if (!strcmp(a, "--agents") && i + 1 < argc) SIM.agents = atoi(argv[++i]);
        else if (!strcmp(a, "--wav") && i + 1 < argc) SIM.wav = argv[++i];
        else if (!strcmp(a, "--store") && i + 1 < argc) SIM.store_path = argv[++i];
        else if (!strcmp(a, "--link-port") && i + 1 < argc) SIM.link_port = atoi(argv[++i]);
        else if (!strcmp(a, "--debug-port") && i + 1 < argc) SIM.debug_port = atoi(argv[++i]);
        else if (!strcmp(a, "--scale") && i + 1 < argc) { SIM.scale = atoi(argv[++i]); s_scale_fixed = true; }
        else {
            usage();
            return 2;
        }
    }
    if (!SIM.board || !sim_select_board(SIM.board)) {
        usage();
        return 2;
    }
    if (SIM.scale < 1) SIM.scale = 1;
    if (SIM.view && !sim_view_open(SIM.view)) return 0; // there's a viewer on this file already, and that's enough
    if (!SIM.store_path) {
        // Beside the binary, so that a restart finds it again.
        static char path[1024];
        char exe[900];
        snprintf(exe, sizeof exe, "%s", argv[0]);
        char *slash = strrchr(exe, '/');
        if (slash) *slash = 0;
        else snprintf(exe, sizeof exe, ".");
        snprintf(path, sizeof path, "%s/store-%s.bin", exe, SIM.board);
        SIM.store_path = path;
    }
    if (SIM.headless) {
        sim_run_ux(); // never returns
        return 0;
    }
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        SimController *controller = [SimController new];
        app.delegate = controller;
        s_controller = controller;
        if (!SIM.view) {
            sim_window_command = window_command;
            pthread_t th;
            pthread_create(&th, NULL, ux_thread, NULL);
        }
        [app run];
    }
    return 0;
}
