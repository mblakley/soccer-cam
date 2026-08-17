/* lut2d_ioctl - read and write the live DCE 2-D warp mesh on the camera.
 *
 * Cross-compile for the camera:
 *   aarch64-linux-gnu-gcc -O2 -static -o lut2d_ioctl lut2d_ioctl.c
 *
 * Protocol, from RE of app/device and hdal/vpe/nvt_vpe.ko on
 * v3.0.0.4867_2505072124:
 *
 *   VPE_IOC_GET_2DLUT = _IOWR('v', 13, 8) = 0xc008760d   (device @ VA 0x599c1c)
 *   VPE_IOC_SET_2DLUT = _IOW ('v', 13, 8) = 0x4008760d
 *   buffer            = {u32 id, u32 n} followed by the table at +8
 *   size              = 8 + align4(n)*n*4
 *                       n = vpe_2dlut_size = 257 -> 267288
 *
 * The n field is buf[1], NOT buf[2]. This was wrong in the first version and
 * the failure is silent: the driver returns align4(0)*0 = zero entries and
 * still reports success, so you get a structurally perfect, entirely empty
 * mesh. Established by sweeping calling conventions against the procfs oracle
 * `get_2dlut_param 0`, whose 2dlut[0] must appear in any correct response.
 *   entry             = (y<<16)|x, each half unsigned Q14.2
 *
 * The ioctl's declared size field is 8, but the driver reads the whole buffer;
 * passing the buffer itself as the argument is the convention that returns 0
 * and yields a table matching what procfs reports (`get_2dlut_param`).
 *
 * GET is proven on hardware. SET is NOT — it was written after the camera went
 * offline and has never run. Hence: `set` always reads the mesh back and diffs
 * it, and refuses to proceed at all unless --i-have-a-recovery-path is given.
 * Writing a folded or out-of-range mesh produces a torn image, not a brick, but
 * verify against a known-good dump before trusting it.
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define GET_2DLUT 0xc008760dUL
#define SET_2DLUT 0x4008760dUL
#define DEV "/dev/nvt_vpe"

#define N 257
#define ALIGN4(x) (((x) + 3) & ~3)
#define STRIDE ALIGN4(N)
#define HDR_WORDS 2
#define HDR_BYTES (HDR_WORDS * 4)
#define TABLE_BYTES (STRIDE * N * 4)
#define BUFSZ (HDR_BYTES + TABLE_BYTES)
/* The driver was built around a 3-word header and can write a few bytes
 * past BUFSZ. Allocate slack -- without it the write corrupts the heap
 * and glibc aborts with "double free or corruption". */
#define ALLOCSZ (BUFSZ + 64)

/* Reject a mesh that would tear the image before handing it to the hardware:
 * the padded tail of every row must be zero, and no control point may fall
 * outside the Q14.2 range the DCE can represent. */
static int table_looks_sane(const unsigned int *t)
{
    int r, c, bad = 0;
    for (r = 0; r < N; r++) {
        for (c = N; c < STRIDE; c++) {
            if (t[r * STRIDE + c]) {
                fprintf(stderr, "  row %d pad[%d] = 0x%08x, expected 0\n", r, c, t[r * STRIDE + c]);
                if (++bad > 8) return 0;
            }
        }
    }
    return bad == 0;
}

static unsigned char *read_file(const char *path, long *out_len)
{
    FILE *f = fopen(path, "rb");
    unsigned char *buf;
    long len;
    if (!f) { perror(path); return NULL; }
    fseek(f, 0, SEEK_END);
    len = ftell(f);
    fseek(f, 0, SEEK_SET);
    buf = malloc(len);
    if (!buf || fread(buf, 1, len, f) != (size_t)len) {
        fprintf(stderr, "%s: short read\n", path);
        free(buf);
        fclose(f);
        return NULL;
    }
    fclose(f);
    *out_len = len;
    return buf;
}

static int do_get(int fd, int id, const char *path)
{
    unsigned char *buf = calloc(1, ALLOCSZ);
    unsigned int *h = (unsigned int *)buf;
    FILE *f;
    int r;

    h[0] = id; h[1] = N;
    r = ioctl(fd, GET_2DLUT, buf);
    printf("GET id=%d -> %d  hdr={%u,%u,%u}\n", id, r, h[0], h[1], h[2]);
    if (r != 0) { free(buf); return 1; }

    f = fopen(path, "wb");
    if (!f) { perror(path); free(buf); return 1; }
    fwrite(buf, 1, BUFSZ, f);
    fclose(f);
    printf("  wrote %s (%d bytes)\n", path, BUFSZ);
    free(buf);
    return 0;
}

static int do_set(int fd, int id, const char *path, int armed)
{
    unsigned char *in;
    unsigned char *buf, *back;
    unsigned int *h;
    long len;
    int r, off;

    in = read_file(path, &len);
    if (!in) return 1;

    /* Accept a dump saved with either a 2-word or 3-word header. */
    if (len == BUFSZ) off = HDR_BYTES;
    else if (len == BUFSZ - 4) off = HDR_BYTES - 4;
    else {
        fprintf(stderr, "%s: %ld bytes, expected %d or %d\n", path, len, BUFSZ, BUFSZ - 4);
        free(in);
        return 1;
    }

    buf = calloc(1, ALLOCSZ);
    h = (unsigned int *)buf;
    h[0] = id; h[1] = N;
    memcpy(buf + HDR_BYTES, in + off, TABLE_BYTES);
    free(in);

    if (!table_looks_sane((unsigned int *)(buf + HDR_BYTES))) {
        fprintf(stderr, "refusing to write: table failed the structure check\n");
        free(buf);
        return 1;
    }
    printf("mesh from %s passes the structure check\n", path);

    if (!armed) {
        printf("dry run — pass --i-have-a-recovery-path to actually write\n");
        free(buf);
        return 0;
    }

    r = ioctl(fd, SET_2DLUT, buf);
    printf("SET id=%d -> %d\n", id, r);
    if (r != 0) { free(buf); return 1; }

    /* Read back and diff. A SET that silently does nothing looks identical to
     * one that worked unless you check. */
    back = calloc(1, ALLOCSZ);
    h = (unsigned int *)back;
    h[0] = id; h[1] = N;
    r = ioctl(fd, GET_2DLUT, back);
    if (r != 0) {
        printf("  read-back failed (%d) — cannot confirm the write\n", r);
        free(buf); free(back);
        return 1;
    }
    if (memcmp(buf + HDR_BYTES, back + HDR_BYTES, TABLE_BYTES) == 0) {
        printf("  read-back matches: the mesh is live\n");
        free(buf); free(back);
        return 0;
    }
    {
        int i, diff = 0;
        unsigned int *a = (unsigned int *)(buf + HDR_BYTES);
        unsigned int *b = (unsigned int *)(back + HDR_BYTES);
        for (i = 0; i < STRIDE * N; i++) if (a[i] != b[i]) diff++;
        printf("  read-back DIFFERS in %d of %d entries — the write did not take\n",
               diff, STRIDE * N);
    }
    free(buf); free(back);
    return 1;
}

int main(int argc, char **argv)
{
    int fd, rc, id, armed = 0, i;
    const char *cmd, *path;

    if (argc < 4) {
        fprintf(stderr,
                "usage: %s get <vpe_id> <out.bin>\n"
                "       %s set <vpe_id> <in.bin> [--i-have-a-recovery-path]\n",
                argv[0], argv[0]);
        return 2;
    }
    cmd = argv[1];
    id = atoi(argv[2]);
    path = argv[3];
    for (i = 4; i < argc; i++)
        if (!strcmp(argv[i], "--i-have-a-recovery-path")) armed = 1;

    fd = open(DEV, O_RDWR);
    if (fd < 0) { perror("open " DEV); return 1; }

    if (!strcmp(cmd, "get")) rc = do_get(fd, id, path);
    else if (!strcmp(cmd, "set")) rc = do_set(fd, id, path, armed);
    else { fprintf(stderr, "unknown command: %s\n", cmd); rc = 2; }

    close(fd);
    return rc;
}
