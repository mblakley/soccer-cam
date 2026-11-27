/* setroi_harness -- execute kflow_videoenc.ko's `setroi` command handler in
 * user space, so a binary patch to it is run before it is ever flashed.
 *
 * The handler is kernel code. Its first real execution would otherwise be on
 * the camera, in kernel mode, where a wrong instruction is an oops in the
 * process that owns the encoder. This links the module itself (it is a plain
 * relocatable object) into a static aarch64 program, supplies the handful of
 * kernel functions the handler calls, and drives it with command strings. The
 * linker applies the module's relocation table on the way, so a malformed
 * relocation entry shows up here as a link error or a wrong address rather than
 * as a module that will not load.
 *
 * What is real: every instruction of Cmd_VdoEnc_SetROI and NMR_VdoEnc_SetROI,
 * and the module's own relocations. What is substituted: sscanf_s (glibc's
 * vsscanf; kwrap's own handling of %hd / %hhd was established statically in
 * docs/ENCODER_ROI_QP.md section 4), printk, the encoder callback, and the
 * stack-protector's `mrs x1, sp_el0`, which is privileged and is emulated from
 * a SIGILL handler. What it cannot show: anything the codec does with the
 * window afterwards.
 *
 * usage: setroi_harness <stock|fix|multiwin>
 * Built and run by verify/test_setroi_patch.sh; exits non-zero on any failure.
 */
#define _GNU_SOURCE
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <ucontext.h>
#include <unistd.h>

/* From the module. */
extern void *gNMRVdoEncObj;               /* .bss+0x2c0: per-path object array   */
extern unsigned g_vdoenc_path_max_count;
extern void NMR_VdoEnc_SetROI(unsigned path, const void *roi);

#define OBJ_STRIDE 0xbe8  /* per-path object size                                */
#define ROI_OFF 0x388     /* ROI struct inside it: 10 x 16 bytes + 8             */
#define ROI_SIZE 0xa8
#define TRIG_STRIDE 0x938 /* NMR_VdoTrig_GetVidEncoder: *(.bss+0x588) + path*this */
#define NPATH 4

typedef struct {
    uint32_t enable;
    uint16_t x, y, w, h;
    int8_t qp;
    uint8_t mode;
    uint16_t pad;
} Win;

static uint8_t *objs, *trig;
static unsigned long fake_task[256]; /* stack canary is read at task+1040 */
static int cb_calls, cb_cmd;
static void *cb_arg;
static char last_print[512];

/* --- what the kernel would provide ------------------------------------------ */

int sscanf_s(const char *s, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int r = vsscanf(s, fmt, ap);
    va_end(ap);
    return r;
}

int printk(const char *fmt, ...)
{
    va_list ap;
    if (fmt[0] == 1 && fmt[1])
        fmt += 2; /* KERN_<level> prefix */
    va_start(ap, fmt);
    vsnprintf(last_print, sizeof(last_print), fmt, ap);
    va_end(ap);
    return 0;
}

int printm2(const char *fmt, ...)
{
    (void)fmt;
    return 0;
}

static long callback(long cmd, void *p, long a, long b)
{
    (void)a;
    (void)b;
    cb_calls++;
    cb_cmd = (int)cmd;
    cb_arg = p;
    return 0;
}

static void on_sigill(int sig, siginfo_t *si, void *ctx)
{
    ucontext_t *uc = ctx;
    (void)sig;
    (void)si;
    if (*(uint32_t *)uc->uc_mcontext.pc == 0xd5384101u) { /* mrs x1, sp_el0 */
        uc->uc_mcontext.regs[1] = (unsigned long)fake_task;
        uc->uc_mcontext.pc += 4;
        return;
    }
    _exit(99);
}

/* --- driving it -------------------------------------------------------------- */

static int fails;

static void check(int ok, const char *what)
{
    printf("  [%s] %s\n", ok ? "ok" : "FAIL", what);
    fails += !ok;
}

static Win *win(unsigned path, unsigned i)
{
    return (Win *)(objs + (size_t)path * OBJ_STRIDE + ROI_OFF) + i;
}

static void reset(void)
{
    memset(objs, 0xAA, (size_t)NPATH * OBJ_STRIDE);
    for (unsigned p = 0; p < NPATH; p++)
        memset(objs + (size_t)p * OBJ_STRIDE + ROI_OFF, 0, ROI_SIZE);
    cb_calls = 0;
    cb_arg = NULL;
    last_print[0] = 0;
}

/* Bytes of the object array outside every ROI struct must never change. */
static int guard_intact(void)
{
    for (unsigned p = 0; p < NPATH; p++)
        for (size_t o = 0; o < OBJ_STRIDE; o++) {
            if (o >= ROI_OFF && o < ROI_OFF + ROI_SIZE)
                continue;
            if (objs[(size_t)p * OBJ_STRIDE + o] != 0xAA)
                return 0;
        }
    return 1;
}

static int is_zero(const void *p, size_t n)
{
    const uint8_t *b = p;
    for (size_t i = 0; i < n; i++)
        if (b[i])
            return 0;
    return 1;
}

static int win_is(const Win *w, unsigned en, unsigned x, unsigned y, unsigned ww, unsigned h, int qp, unsigned mode)
{
    return w->enable == en && w->x == x && w->y == y && w->w == ww && w->h == h && w->qp == qp && w->mode == mode;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: setroi_harness <stock|fix|multiwin>\n");
        return 2;
    }
    int stock = !strcmp(argv[1], "stock"), multi = !strcmp(argv[1], "multiwin");
    if (!stock && !multi && strcmp(argv[1], "fix")) {
        fprintf(stderr, "unknown variant %s\n", argv[1]);
        return 2;
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = on_sigill;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGILL, &sa, NULL);
    fake_task[1040 / 8] = 0x5ca1ab1e0ddba11ul;

    objs = malloc((size_t)NPATH * OBJ_STRIDE);
    trig = calloc(NPATH, TRIG_STRIDE);
    if (!objs || !trig)
        return 2;
    gNMRVdoEncObj = objs;
    /* The encoder-object array pointer is a file-local at .bss+0x588, i.e.
     * 0x2c8 bytes past gNMRVdoEncObj. The callback slot is at +24 in each. */
    *(void **)((char *)&gNMRVdoEncObj + 0x2c8) = trig;
    for (unsigned p = 0; p < NPATH; p++)
        *(void **)(trig + (size_t)p * TRIG_STRIDE + 24) = (void *)callback;
    g_vdoenc_path_max_count = NPATH;

    /* Cmd_VdoEnc_SetROI is static; it starts 0xc0 past NMR_VdoEnc_SetROI. */
    int (*setroi)(char *) = (int (*)(char *))((char *)NMR_VdoEnc_SetROI + 0xc0);
    char buf[128];
#define RUN(s) (strcpy(buf, s), setroi(buf))

    printf("variant: %s\n", argv[1]);

    /* 1. One window on index 0. */
    reset();
    int rc = RUN("0 0 1 -6 1 0 760 7680 900");
    printf("  kernel echo: %s", last_print[0] ? last_print : "(none)\n");
    if (stock) {
        /* The bug this whole effort started from: QP and QPMode come out 0. */
        check(win_is(win(0, 0), 1, 0, 760, 7680, 900, 0, 0), "stock module loses QP and QPMode (they read back 0)");
    } else {
        check(win_is(win(0, 0), 1, 0, 760, 7680, 900, -6, 1), "window 0 carries QP -6, mode 1 and the rectangle");
        check(strstr(last_print, "-6") != NULL, "the kernel's echo shows the QP that was parsed");
    }
    check(rc == 1, "handler reports success");
    check(cb_calls == 1 && cb_cmd == 10 && cb_arg == (void *)win(0, 0), "encoder told 'ROI changed' (cmd 10) once");
    check(is_zero(win(0, 1), 9 * sizeof(Win) + 8), "windows 1..9 and the trailer are untouched (zero)");
    check(guard_intact(), "nothing outside the ROI struct was written");

    if (!stock) {
        /* 2. A second window on index 3. */
        rc = RUN("0 3 1 4 1 64 1472 7552 640");
        if (multi) {
            check(win_is(win(0, 3), 1, 64, 1472, 7552, 640, 4, 1), "index 3 lands in window 3");
            check(win_is(win(0, 0), 1, 0, 760, 7680, 900, -6, 1), "window 0 survives a write to window 3");
            check(strstr(last_print, "1472") != NULL, "echo shows the window just parsed, not a stale slot");
            check(cb_calls == 2 && cb_arg == (void *)win(0, 0), "encoder told again, with the whole struct");
        } else {
            check(win_is(win(0, 0), 1, 64, 1472, 7552, 640, 4, 1) && is_zero(win(0, 1), 9 * sizeof(Win)),
                  "single-window module: index is ignored, window 0 is replaced, the rest zeroed");
        }
        check(guard_intact(), "nothing outside the ROI struct was written");
    }

    if (multi) {
        /* 3. Last valid index, and a different path. */
        rc = RUN("0 9 1 -3 1 1 2 3 4");
        check(rc == 1 && win_is(win(0, 9), 1, 1, 2, 3, 4, -3, 1), "index 9 (the last) works");
        rc = RUN("2 1 1 -8 1 10 20 30 40");
        check(rc == 1 && win_is(win(2, 1), 1, 10, 20, 30, 40, -8, 1) && cb_arg == (void *)win(2, 0),
              "path 2 writes path 2's struct and nobody else's");
        check(win_is(win(0, 0), 1, 0, 760, 7680, 900, -6, 1) && win_is(win(0, 3), 1, 64, 1472, 7552, 640, 4, 1),
              "path 0's windows are unaffected by path 2");

        /* 4. Disable one window. */
        rc = RUN("0 3 0 0 0 0 0 1 1");
        check(rc == 1 && win(0, 3)->enable == 0 && win(0, 0)->enable == 1 && win(0, 9)->enable == 1,
              "disabling window 3 leaves 0 and 9 enabled");

        /* 5. Refusals: nothing written, encoder not poked, returns 0. */
        uint8_t before[NPATH * OBJ_STRIDE];
        memcpy(before, objs, sizeof(before));
        int calls = cb_calls;
        rc = RUN("0 10 1 -6 1 0 0 64 64");
        check(rc == 0 && cb_calls == calls && !memcmp(before, objs, sizeof(before)), "index 10 is refused, nothing touched");
        rc = RUN("0 999999999 1 -6 1 0 0 64 64");
        check(rc == 0 && cb_calls == calls && !memcmp(before, objs, sizeof(before)),
              "index 999999999 is refused, nothing touched, no wild read");
        rc = RUN("0 -1 1 -6 1 0 0 64 64");
        check(rc == 0 && cb_calls == calls && !memcmp(before, objs, sizeof(before)), "index -1 is refused");
        rc = RUN("7 0 1 -6 1 0 0 64 64");
        check(rc == 0 && cb_calls == calls && !memcmp(before, objs, sizeof(before)), "path beyond the count is refused");
        check(guard_intact(), "nothing outside the ROI struct was written");
    } else {
        /* The unpatched tail indexes its read-back by an unchecked RoiIndex. */
        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) {
            RUN("0 999999999 1 -6 1 0 0 64 64");
            _exit(0);
        }
        int st = 0;
        waitpid(pid, &st, 0);
        printf("  [note] index 999999999 on this module: %s\n",
               WIFSIGNALED(st) ? "CRASHES (wild read off the stack; on the camera, a kernel oops)" : "survived");
    }

    printf("%s: %s\n", argv[1], fails ? "FAILED" : "passed");
    return fails ? 1 : 0;
}
