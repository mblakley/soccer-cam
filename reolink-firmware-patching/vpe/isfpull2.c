/* isfpull2 -- pull one frame descriptor from an ISF out-port and release it
 * immediately, decoding the key fields. Freestanding aarch64.
 *
 * DIFFERENCE FROM isfpull.c (this is the whole point):
 *   arg[4] is the PULL TIMEOUT IN MILLISECONDS, not a "flags" word.
 *
 * Recovered from `device` (statically-linked Novatek HDAL),
 * hd_videoproc_pull_out_buf @0x5b65b0:
 *
 *   w23 = (HD_PATH_ID >> 16) - 0x236f      ; ISF unit  (0x2400-0x236f = 0x91)
 *   w0  = (HD_PATH_ID & 0xff) - 1          ; ISF port  (1-based -> 0-based)
 *   arg[1] = (w23 << 16) | w0              ; ISF path_id
 *   arg[2..3] = &desc(296)   memset(0)     ; PURE OUTPUT
 *   arg[4] = timeout_ms                    ; <-- caller's 3rd arg
 *   ioctl(fd, 0xc020490c, arg)
 *
 * The vendor stitch loop (bc_stitch_main @0x481800) pulls the two per-sensor
 * ports with timeout 200 ms. Its VSP-out thread pulls with timeout 0 and treats
 * BOTH -56 and -15 as "no frame yet, sleep 20 ms and retry" (0x481e8c:
 * cmn w0,#0x38 / ccmn w0,#0xf) -- i.e. -56 is the normal non-blocking
 * would-block code, not a structural refusal.
 *
 * usage: isfpull2 <timeout_ms_dec> <path_hex> [<path_hex> ...]
 */
#define SYS_openat 56
#define SYS_close  57
#define SYS_ioctl  29
#define SYS_write  64
#define SYS_exit   93
#define AT_FDCWD  -100

static long sys3(long n,long a,long b,long c){
    register long x8 __asm__("x8")=n; register long x0 __asm__("x0")=a;
    register long x1 __asm__("x1")=b; register long x2 __asm__("x2")=c;
    __asm__ volatile("svc #0":"+r"(x0):"r"(x8),"r"(x1),"r"(x2):"memory"); return x0; }
static long sys4(long n,long a,long b,long c,long d){
    register long x8 __asm__("x8")=n; register long x0 __asm__("x0")=a;
    register long x1 __asm__("x1")=b; register long x2 __asm__("x2")=c; register long x3 __asm__("x3")=d;
    __asm__ volatile("svc #0":"+r"(x0):"r"(x8),"r"(x1),"r"(x2),"r"(x3):"memory"); return x0; }

static unsigned slen(const char*s){unsigned n=0;while(s[n])n++;return n;}
static void out(const char*s){sys3(SYS_write,1,(long)s,slen(s));}
static void hex32(unsigned v,char*b){static const char d[]="0123456789abcdef";b[0]='0';b[1]='x';for(int i=0;i<8;i++)b[2+i]=d[(v>>((7-i)*4))&0xf];b[10]=0;}
static void dec(long v,char*b){char t[24];int n=0,neg=0;if(v<0){neg=1;v=-v;}if(!v)t[n++]='0';while(v){t[n++]='0'+(v%10);v/=10;}int i=0;if(neg)b[i++]='-';while(n)b[i++]=t[--n];b[i]=0;}
static unsigned parse(const char*s){unsigned v=0;if(s[0]=='0'&&(s[1]=='x'||s[1]=='X'))s+=2;while(*s){char c=*s++;unsigned d;if(c>='0'&&c<='9')d=c-'0';else if(c>='a'&&c<='f')d=c-'a'+10;else if(c>='A'&&c<='F')d=c-'A'+10;else break;v=v*16+d;}return v;}
static unsigned pdec(const char*s){unsigned v=0;while(*s>='0'&&*s<='9')v=v*10+(*s++-'0');return v;}
static void kv(const char*k,unsigned v){char b[24];out(k);hex32(v,b);out(b);out(" (");dec((int)v,b);out(b);out(")\n");}

void _c_start(long*sp){
    long argc=sp[0]; char**argv=(char**)(sp+1); char b[24];
    unsigned desc[0x128/4];
    unsigned arg[8];
    if(argc<3){out("usage: isfpull2 <timeout_ms> <path_hex> [...]\n");sys3(SYS_exit,2,0,0);}
    unsigned tmo=pdec(argv[1]);

    int fd=(int)sys4(SYS_openat,AT_FDCWD,(long)"/dev/isf_flow0",2,0);
    if(fd<0){out("open isf_flow0 rc=");dec(fd,b);out(b);out("\n");sys3(SYS_exit,1,0,0);}

    for(long ai=2; ai<argc; ai++){
        unsigned path=parse(argv[ai]);
        for(int i=0;i<0x128/4;i++) desc[i]=0;
        for(int i=0;i<8;i++) arg[i]=0;
        unsigned long dp=(unsigned long)desc;
        arg[1]=path; arg[2]=(unsigned)(dp&0xffffffff); arg[3]=(unsigned)(dp>>32);
        arg[4]=tmo;                       /* <-- TIMEOUT MS (was wrongly 0/"flags") */

        long rp=sys3(SYS_ioctl,fd,(long)0xc020490c,(long)arg);
        unsigned rr_pull=arg[0];
        /* release immediately -- hold the depth-1 buffer for microseconds only */
        long rr=sys3(SYS_ioctl,fd,(long)0xc020490a,(long)arg);
        unsigned rr_rel=arg[0];

        out("=== path ");hex32(path,b);out(b);out(" timeout_ms=");dec(tmo,b);out(b);out("\n");
        out("pull ioctl_rc=");dec(rp,b);out(b);
        out(" arg0=");dec((int)rr_pull,b);out(b);
        out(" | release ioctl_rc=");dec(rr,b);out(b);
        out(" arg0=");dec((int)rr_rel,b);out(b);out("\n");
        if(rr_pull==0){
            kv("  magic[0]      = ",desc[0]);
            kv("  pxlfmt[60]    = ",desc[60/4]);
            kv("  width[64]     = ",desc[64/4]);
            kv("  height[68]    = ",desc[68/4]);
            kv("  lineoff0[88]  = ",desc[88/4]);
            kv("  lineoff1[92]  = ",desc[92/4]);
            kv("  phys_Y[168]   = ",desc[168/4]);
            kv("  phys_UV[176]  = ",desc[176/4]);
        }
        out("desc(0x128):\n");
        for(int i=0;i<0x128/4;i++){
            out("[");dec(i*4,b);out(b);out("]=");hex32(desc[i],b);out(b);
            if((i&3)==3) out("\n"); else out(" ");
        }
        out("\n");
    }
    sys3(SYS_close,fd,0,0);
    sys3(SYS_exit,0,0,0);
}
__asm__(".global _start\n_start:\n  mov x0, sp\n  b _c_start\n");
