/* pemdump -- read a physical DRAM range via /dev/mem and write it to a file.
 * Freestanding aarch64, no libc (modelled on runtime/isfbind.c), so it stays
 * ~3 KB and can be wget'd to /mnt/tmp.
 *
 * Route A of the Duo 3 pre-stitch capture: the VIDEOPROC 0/1 out buffers are
 * not kernel-bound (bind_dest (null)); bc_stitch_main pulls them in userspace.
 * Their physical addresses are published by /proc/hdal/comm/info. We mmap
 * /dev/mem read-only over the block and copy it out -- no ISF pull, so no
 * competition with `device` for the single (depth 1) buffer. Tearing is
 * possible (we read while it is written) but irrelevant for a static scene.
 *
 * usage:  pemdump <phys_hex> <len_hex> <outpath>
 * e.g.    pemdump 0x1c105000 0xbdd800 /mnt/sda/dump/frame.bin
 */

#define SYS_openat 56
#define SYS_close  57
#define SYS_write  64
#define SYS_mmap   222
#define SYS_munmap 215
#define SYS_exit   93

#define O_RDONLY 0
#define O_WRONLY 1
#define O_CREAT  0100
#define O_TRUNC  01000
#define PROT_READ  1
#define MAP_SHARED 1
#define AT_FDCWD  -100

static long sys6(long n, long a, long b, long c, long d, long e, long f)
{
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x4 __asm__("x4") = e;
    register long x5 __asm__("x5") = f;
    __asm__ volatile("svc #0"
                     : "+r"(x0)
                     : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5)
                     : "memory");
    return x0;
}
#define sys3(n,a,b,c)   sys6(n,a,b,c,0,0,0)

static unsigned slen(const char *s){unsigned n=0;while(s[n])n++;return n;}
static void out(const char *s){sys3(SYS_write,2,(long)s,slen(s));}
static void dec(long v,char *b){char t[24];int n=0,neg=0;if(v<0){neg=1;v=-v;}if(!v)t[n++]='0';while(v){t[n++]='0'+(v%10);v/=10;}int i=0;if(neg)b[i++]='-';while(n)b[i++]=t[--n];b[i]=0;}
static void emit(const char*m,long v){char b[24];out(m);dec(v,b);out(b);out("\n");}

static unsigned long parse(const char *s)
{
    unsigned long v = 0;
    if (s[0]=='0' && (s[1]=='x'||s[1]=='X')) s += 2;
    while (*s) {
        char c = *s++; unsigned d;
        if (c>='0'&&c<='9') d=c-'0';
        else if (c>='a'&&c<='f') d=c-'a'+10;
        else if (c>='A'&&c<='F') d=c-'A'+10;
        else break;
        v = v*16 + d;
    }
    return v;
}

void _c_start(long *sp)
{
    long argc = sp[0];
    char **argv = (char **)(sp + 1);
    if (argc < 4) { out("usage: pemdump <phys_hex> <len_hex> <outpath>\n"); sys3(SYS_exit,2,0,0); }

    unsigned long pa  = parse(argv[1]);
    unsigned long len = parse(argv[2]);
    const char *outp  = argv[3];

    int fdm = (int)sys6(SYS_openat, AT_FDCWD, (long)"/dev/mem", O_RDONLY, 0,0,0);
    if (fdm < 0) { emit("open /dev/mem rc=", fdm); sys3(SYS_exit,1,0,0); }

    long p = sys6(SYS_mmap, 0, (long)len, PROT_READ, MAP_SHARED, fdm, (long)pa);
    if (p < 0 && p > -4096) { emit("mmap rc=", p); sys3(SYS_exit,1,0,0); }

    int fdo = (int)sys6(SYS_openat, AT_FDCWD, (long)outp, O_WRONLY|O_CREAT|O_TRUNC, 0644, 0,0);
    if (fdo < 0) { emit("open out rc=", fdo); sys3(SYS_exit,1,0,0); }

    unsigned long done = 0;
    const char *base = (const char *)p;
    while (done < len) {
        unsigned long chunk = len - done;
        if (chunk > 1u<<20) chunk = 1u<<20;
        long w = sys3(SYS_write, fdo, (long)(base + done), (long)chunk);
        if (w <= 0) { emit("write rc=", w); sys3(SYS_exit,1,0,0); }
        done += (unsigned long)w;
    }
    sys3(SYS_close, fdo, 0, 0);
    sys6(SYS_munmap, p, (long)len, 0,0,0,0);
    sys3(SYS_close, fdm, 0, 0);
    emit("ok bytes=", (long)done);
    sys3(SYS_exit, 0, 0, 0);
}

__asm__(".global _start\n_start:\n  mov x0, sp\n  b _c_start\n");
