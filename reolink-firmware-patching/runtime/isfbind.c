/* isfbind -- issue ISF bind ioctls on /dev/isf_flow0, freestanding aarch64.
 *
 * Protocol (decoded from kflow_common.ko, see docs/ISF_PARAM_MAP.md):
 *   0xc00c4901 isf_unit_set_bind  arg = {u32 ret, u32 src_path_id, u32 dst_path_id}
 *   0xc00c4902 isf_unit_get_bind  arg = {u32 ret, u32 path_id,     u32 result}
 *   0xc00c4903 isf_unit_set_state arg = {u32 ret, u32 path_id,     u32 state}
 *   0xc00c4904 isf_unit_get_state arg = {u32 ret, u32 path_id,     u32 state}
 * isf_flow_drv_ioctl copies 12 bytes then `ldp w1,w2,[sp,#68]`, so words 1 and
 * 2 are the two operands and word 0 is the driver's return slot.
 *
 * set_bind validates src port < 0x80 (an OUT port) and dst port in 0x80..0xff
 * (an IN port); anything else is rejected before the unit is touched.
 *
 * No libc: -nostdlib keeps it ~2 KB so it can be pushed to /mnt/tmp over wget.
 *
 * usage:  isfbind get  <path_id>
 *         isfbind set  <src_path_id> <dst_path_id>
 *         isfbind state <path_id>
 */

#define SYS_openat 56
#define SYS_close  57
#define SYS_ioctl  29
#define SYS_write  64
#define SYS_exit   93

static long sys3(long n, long a, long b, long c)
{
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2) : "memory");
    return x0;
}

static long sys4(long n, long a, long b, long c, long d)
{
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory");
    return x0;
}

static unsigned slen(const char *s) { unsigned n = 0; while (s[n]) n++; return n; }
static void out(const char *s) { sys3(SYS_write, 1, (long)s, slen(s)); }

static void hex32(unsigned v, char *b)
{
    static const char d[] = "0123456789abcdef";
    b[0] = '0'; b[1] = 'x';
    for (int i = 0; i < 8; i++) b[2 + i] = d[(v >> ((7 - i) * 4)) & 0xf];
    b[10] = 0;
}

static void dec(long v, char *b)
{
    char t[24]; int n = 0, neg = 0;
    if (v < 0) { neg = 1; v = -v; }
    if (!v) t[n++] = '0';
    while (v) { t[n++] = '0' + (v % 10); v /= 10; }
    int i = 0;
    if (neg) b[i++] = '-';
    while (n) b[i++] = t[--n];
    b[i] = 0;
}

static unsigned parse(const char *s)
{
    unsigned v = 0;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) s += 2;
    while (*s) {
        char c = *s++;
        unsigned d;
        if (c >= '0' && c <= '9') d = c - '0';
        else if (c >= 'a' && c <= 'f') d = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') d = c - 'A' + 10;
        else break;
        v = v * 16 + d;
    }
    return v;
}

static int eq(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

void _c_start(long *sp)
{
    long argc = sp[0];
    char **argv = (char **)(sp + 1);
    char buf[16];

    int fd = (int)sys4(SYS_openat, -100 /*AT_FDCWD*/, (long)"/dev/isf_flow0", 2 /*O_RDWR*/, 0);
    if (fd < 0) { out("open /dev/isf_flow0 failed rc="); dec(fd, buf); out(buf); out("\n"); sys3(SYS_exit, 1, 0, 0); }

    unsigned a[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    unsigned long cmd = 0;
    int isparam = 0;

    if (argc >= 3 && eq(argv[1], "get")) {
        cmd = 0xc00c4902; a[1] = parse(argv[2]);
    } else if (argc >= 3 && eq(argv[1], "state")) {
        cmd = 0xc00c4904; a[1] = parse(argv[2]);
    } else if (argc >= 4 && eq(argv[1], "set")) {
        cmd = 0xc00c4901; a[1] = parse(argv[2]); a[2] = parse(argv[3]);
    } else if (argc >= 4 && eq(argv[1], "getparam")) {
        /* 32-byte form: [1]=path_id [2]=param_id [4..5]=value [6]=len(0=scalar) */
        cmd = 0xc0204906; a[1] = parse(argv[2]); a[2] = parse(argv[3]); isparam = 1;
    } else if (argc >= 5 && eq(argv[1], "setparam")) {
        cmd = 0xc0204905; a[1] = parse(argv[2]); a[2] = parse(argv[3]);
        a[4] = parse(argv[4]); isparam = 1;
    } else {
        out("usage: isfbind get <path> | state <path> | set <src> <dst>\n"
            "       isfbind getparam <path> <param> | setparam <path> <param> <val>\n");
        sys3(SYS_exit, 2, 0, 0);
    }

    long rc = sys3(SYS_ioctl, fd, (long)cmd, (long)a);

    out("cmd="); hex32((unsigned)cmd, buf); out(buf);
    out(" arg1="); hex32(a[1], buf); out(buf);
    out(" arg2="); hex32(a[2], buf); out(buf);
    if (isparam) { out(" val="); hex32(a[4], buf); out(buf); }
    out(" ret_slot="); hex32(a[0], buf); out(buf);
    out(" rc="); dec(rc, buf); out(buf);
    out("\n");

    sys3(SYS_close, fd, 0, 0);
    sys3(SYS_exit, rc == 0 ? 0 : 1, 0, 0);
}

__asm__(
    ".global _start\n"
    "_start:\n"
    "  mov x0, sp\n"
    "  b _c_start\n");
