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
 * GET and SET are both proven on hardware (2026-08-17): the factory mesh was
 * dumped, written back as a verified no-op, then a modified mesh was written,
 * observed to change the image, and the factory mesh restored. `set` still
 * reads the mesh back and diffs it, and still refuses without
 * --i-have-a-recovery-path.
 *
 * `compose` is the third subcommand and the reason this tool exists on the
 * camera at all: there is no python or perl on the device, so the boot hook
 * cannot compose in a script. It applies a per-row seam shear to a factory
 * mesh, and its arithmetic is a line-for-line mirror of
 * `lut2d.py:compose_correction` -- `tests/test_lut2d_compose.py` cross-checks
 * the two for byte-identical output, so they cannot drift.
 *
 * NEVER GENERATE, ALWAYS COMPOSE. The factory mesh is this physical unit's
 * stitch calibration, regenerated from CamStitchPara (mtd11) at every boot. A
 * mesh built from a parametric model discards it and cannot be recovered
 * without the vendor's optimiser. `compose` takes a factory dump as input; it
 * has no mode that manufactures a mesh.
 */
#include <fcntl.h>
#include <math.h>
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

/* The driver writes PAST the end of an exact-size buffer, so every allocation
 * handed to the ioctl carries slack. Only the first BUFSZ bytes are ever read
 * back or written to a file, so the slack is invisible everywhere else.
 *
 * Root cause: the driver is built around a THREE-word header {id, reserved, n}
 * and writes align4(n)*n entries after it. We send the two-word layout
 * {id, n} -- the one that actually returns live data -- so its last write
 * lands past the end of a BUFSZ allocation, on glibc chunk metadata.
 *
 * This is not theoretical, and it is worth knowing both faces of it because
 * they look like different bugs. With exact-size allocations:
 *   - `get` returns a CORRECT mesh and then aborts at exit with
 *     "double free or corruption (out)" -- one allocation, damage in the top
 *     chunk. It looks like the ioctl failed when it did not.
 *   - `set` aborts with "double free or corruption (!prev)" at the first
 *     free() after the read-back -- two allocations, so glibc sees the smashed
 *     header of the second one.
 * Both observed 2026-08-17 on v3.0.0.4867_2505072124. */
#define IOCTL_SLACK 4096
#define ALLOCSZ (BUFSZ + IOCTL_SLACK)

/* Geometry of the warped half and the composition gates. Keep in step with the
 * constants of the same name in lut2d.py -- the cross-check test asserts the
 * two implementations agree, but only for meshes it feeds them. */
#define DST_W 3840.0
#define DST_H 2160.0
#define FRAC_SCALE_C 4        /* Q14.2 -- quarter pixels */
#define COORD_MAX_PX 16383.75
#define MAX_ABS_DX 64.0
#define MIN_MONOTONIC 0.95
#define MAX_CLAMP_FRACTION 0.02
#define PROTECTED_SEAM_COLS 32
#define MAX_ANCHORS 64

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

/* ---- compose: factory mesh + per-row shear -> production mesh ------------
 *
 * THE SIGN CONVENTION, ONCE (the same paragraph as lut2d.py).
 *
 * dx(y) means: the pixels the RIGHT half must move RIGHT, at row y, to
 * register with the left. The mesh warps the LEFT half, so it realises the
 * same relative displacement with the opposite sense -- the left half moves
 * LEFT by dx. Moving rendered content left by d destination px means the
 * destination pixel at u must show what was at u+d, so
 * M_new.x(u,v) = M.x(u,v) + d * dM.x/du. Two sign flips that cancel: the
 * increment is +d*s, not -d*s. Getting it backwards doubles the seam error.
 *
 * s = dM.x/du is the local source-px-per-destination-px rate, computed by
 * finite-differencing the factory mesh, so it is always current. It is not 1:
 * on this unit it runs 0.60..1.08, and 0.70 at the seam column.
 */

/* Quarter-pixel quantisation, halves away from zero. lut2d.py:quantise
 * implements exactly this; Python's built-in round() does NOT (it is banker's
 * rounding), which is why that module spells the rule out. */
static int quantise(double px)
{
    double scaled = px * (double)FRAC_SCALE_C;
    return scaled >= 0.0 ? (int)floor(scaled + 0.5) : -(int)floor(-scaled + 0.5);
}

struct anchors {
    int n;
    double y[MAX_ANCHORS];
    double dx[MAX_ANCHORS];
    double src_w, src_h, seam_x;
    unsigned int baseline_crc32;
    int have_baseline;
    char id[64];
};

static double interp_dx(const struct anchors *a, double y)
{
    int i;
    if (y <= a->y[0]) return a->dx[0];
    if (y >= a->y[a->n - 1]) return a->dx[a->n - 1];
    for (i = 1; i < a->n; i++) {
        if (y <= a->y[i]) {
            double y0 = a->y[i - 1], y1 = a->y[i];
            if (y1 == y0) return a->dx[i];
            return a->dx[i - 1] + (a->dx[i] - a->dx[i - 1]) * (y - y0) / (y1 - y0);
        }
    }
    return a->dx[a->n - 1];
}

/* CRC32 (the ordinary reflected IEEE polynomial, same as zlib.crc32) over the
 * table only -- the header carries the VPE id and n, which the reader stamps
 * itself, so two dumps of the same mesh must compare equal regardless of how
 * they were saved. This is a change-detector, not a security property; the
 * device has no sha256 and does not need one for this. */
static unsigned int crc32_buf(const unsigned char *p, unsigned long len)
{
    static unsigned int tab[256];
    static int built = 0;
    unsigned int c;
    unsigned long i;
    int k;
    if (!built) {
        for (i = 0; i < 256; i++) {
            c = (unsigned int)i;
            for (k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320U ^ (c >> 1)) : (c >> 1);
            tab[i] = c;
        }
        built = 1;
    }
    c = 0xFFFFFFFFU;
    for (i = 0; i < len; i++) c = tab[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFU;
}

static int parse_anchors(const char *path, struct anchors *a)
{
    FILE *f = fopen(path, "r");
    char line[512];
    if (!f) { perror(path); return 0; }
    memset(a, 0, sizeof(*a));
    a->src_w = 2.0 * DST_W;
    a->src_h = DST_H;
    a->seam_x = DST_W;
    while (fgets(line, sizeof(line), f)) {
        char k[64];
        double v1, v2;
        char *p = line;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '\n' || *p == '\r' || *p == '\0') continue;
        if (*p == '#') {
            unsigned int crc;
            char idbuf[64];
            if (sscanf(p + 1, " src %lf %lf seam %lf", &v1, &v2, &a->seam_x) == 3) {
                a->src_w = v1; a->src_h = v2;
            } else if (sscanf(p + 1, " src %lf %lf", &v1, &v2) == 2) {
                a->src_w = v1; a->src_h = v2;
            } else if (sscanf(p + 1, " baseline_crc32 %x", &crc) == 1) {
                a->baseline_crc32 = crc; a->have_baseline = 1;
            } else if (sscanf(p + 1, " seam_calibration/2 %63s", idbuf) == 1) {
                size_t idlen = strlen(idbuf);
                if (idlen > sizeof(a->id) - 1) idlen = sizeof(a->id) - 1;
                memcpy(a->id, idbuf, idlen);
                a->id[idlen] = '\0';
            }
            continue;
        }
        if (sscanf(p, "%63s %lf %lf", k, &v1, &v2) != 3 || strcmp(k, "dx")) {
            fprintf(stderr, "unparseable anchors line: %s", line);
            fclose(f);
            return 0;
        }
        if (a->n >= MAX_ANCHORS) {
            fprintf(stderr, "too many anchors (max %d)\n", MAX_ANCHORS);
            fclose(f);
            return 0;
        }
        a->y[a->n] = v1;
        a->dx[a->n] = v2;
        a->n++;
    }
    fclose(f);
    if (a->n < 1) { fprintf(stderr, "%s: no dx lines\n", path); return 0; }
    if (a->src_w <= 0.0 || a->src_h <= 0.0) {
        fprintf(stderr, "bad source geometry %.1fx%.1f\n", a->src_w, a->src_h);
        return 0;
    }
    /* Rescale panorama-space anchors into one half's destination space --
     * lut2d.py:scale_anchors. Both factors are 1.0 in the shipping geometry. */
    {
        double ys = DST_H / a->src_h, xs = (2.0 * DST_W) / a->src_w;
        int i;
        for (i = 0; i < a->n; i++) { a->y[i] *= ys; a->dx[i] *= xs; }
    }
    return 1;
}

static int anchors_sane(const struct anchors *a)
{
    int i;
    double worst = 0.0;
    for (i = 1; i < a->n; i++) {
        if (a->y[i] <= a->y[i - 1]) {
            fprintf(stderr, "anchors must be strictly increasing in y: %.1f then %.1f\n",
                    a->y[i - 1], a->y[i]);
            return 0;
        }
    }
    for (i = 0; i < a->n; i++) {
        double m = a->dx[i] < 0 ? -a->dx[i] : a->dx[i];
        if (m > worst) worst = m;
    }
    if (worst > MAX_ABS_DX) {
        fprintf(stderr, "|dx| = %.2f px exceeds the %.0f px limit; nothing physical "
                        "needs that, so the anchor file is corrupt\n", worst, MAX_ABS_DX);
        return 0;
    }
    return 1;
}

static int do_compose(const char *fac_path, const char *anch_path, const char *out_path,
                      int require_baseline)
{
    unsigned char *in, *out;
    unsigned int *ft, *nt;
    struct anchors a;
    long len;
    int off, row, col, rc = 1;
    long clamped = 0, changed = 0;
    int clamp_col_min = N, clamp_col_max = -1;
    double du = (DST_W - 1.0) / (double)(N - 1);
    double dv = (DST_H - 1.0) / (double)(N - 1);
    double max_disp = 0.0, s_lo = 1e9, s_hi = -1e9, max_span_delta = 0.0, span_bound = 0.0;
    long mono_ok = 0, mono_tot = 0;
    unsigned int fac_crc;

    if (!parse_anchors(anch_path, &a)) return 1;
    if (!anchors_sane(&a)) return 1;

    in = read_file(fac_path, &len);
    if (!in) return 1;
    if (len == BUFSZ) off = HDR_BYTES;
    else if (len == BUFSZ - 4) off = HDR_BYTES - 4;
    else {
        fprintf(stderr, "%s: %ld bytes, expected %d or %d\n", fac_path, len, BUFSZ, BUFSZ - 4);
        free(in);
        return 1;
    }
    ft = (unsigned int *)(in + off);

    /* Liveness before anything else. An all-zero table passes every structural
     * check by construction; the empty-read failure is real and silent. */
    {
        long live = 0;
        for (row = 0; row < N; row++)
            for (col = 0; col < N; col++)
                if (ft[row * STRIDE + col]) live++;
        if (live < (long)(N * N) * 99 / 100) {
            fprintf(stderr, "%s is not a live mesh: %ld of %d control points are zero\n",
                    fac_path, (long)N * N - live, N * N);
            free(in);
            return 1;
        }
    }

    fac_crc = crc32_buf((const unsigned char *)ft, (unsigned long)TABLE_BYTES);
    if (a.have_baseline && fac_crc != a.baseline_crc32) {
        fprintf(stderr, "baseline mismatch: anchors were composed against %08x, "
                        "this mesh is %08x\n", a.baseline_crc32, fac_crc);
        if (require_baseline) { free(in); return 1; }
        /* Not fatal by default, and deliberately so: the shear is a property of
         * the physical lens pair, not of any one mesh, so re-composing it onto
         * whatever the firmware generated this boot is exactly right. Making it
         * fatal would mean one SetStitch permanently disables the boot hook. */
        fprintf(stderr, "  composing onto the current mesh anyway (see the note in do_compose)\n");
    }

    out = calloc(1, BUFSZ);
    if (!out) { fprintf(stderr, "out of memory\n"); free(in); return 1; }
    memcpy(out + HDR_BYTES, ft, TABLE_BYTES);
    nt = (unsigned int *)(out + HDR_BYTES);

    for (row = 0; row < N; row++) {
        double xs[N], d, row_s_lo = 1e9, row_s_hi = -1e9;
        double first_unclamped = 0.0, last_unclamped = 0.0, span_before;
        d = interp_dx(&a, (double)row * dv);
        for (col = 0; col < N; col++)
            xs[col] = (double)(ft[row * STRIDE + col] & 0xFFFF) / (double)FRAC_SCALE_C;
        span_before = xs[N - 1] - xs[0];
        for (col = 0; col < N; col++) {
            double s, disp, nx;
            int xi;
            if (col == 0)          s = (xs[1] - xs[0]) / du;
            else if (col == N - 1) s = (xs[N - 1] - xs[N - 2]) / du;
            else                   s = (xs[col + 1] - xs[col - 1]) / (2.0 * du);
            if (s < row_s_lo) row_s_lo = s;
            if (s > row_s_hi) row_s_hi = s;
            disp = d * s;
            if (fabs(disp) > max_disp) max_disp = fabs(disp);
            nx = xs[col] + disp;
            if (col == 0) first_unclamped = nx;
            else if (col == N - 1) last_unclamped = nx;
            if (nx < 0.0) {
                nx = 0.0; clamped++;
                if (col < clamp_col_min) clamp_col_min = col;
                if (col > clamp_col_max) clamp_col_max = col;
            } else if (nx > COORD_MAX_PX) {
                nx = COORD_MAX_PX; clamped++;
                if (col < clamp_col_min) clamp_col_min = col;
                if (col > clamp_col_max) clamp_col_max = col;
            }
            xi = quantise(nx);
            nt[row * STRIDE + col] = (ft[row * STRIDE + col] & 0xFFFF0000U) | (unsigned int)xi;
            if (nt[row * STRIDE + col] != ft[row * STRIDE + col]) changed++;
        }
        if (row_s_lo < s_lo) s_lo = row_s_lo;
        if (row_s_hi > s_hi) s_hi = row_s_hi;
        {
            double sd = fabs((last_unclamped - first_unclamped) - span_before);
            double hr = fabs(d) * (row_s_hi - row_s_lo) + 0.5;
            if (sd > max_span_delta) max_span_delta = sd;
            if (hr > span_bound) span_bound = hr;
        }
    }

    for (row = 0; row < N; row++)
        for (col = 0; col < N - 1; col++) {
            mono_tot++;
            if ((nt[row * STRIDE + col + 1] & 0xFFFF) >= (nt[row * STRIDE + col] & 0xFFFF))
                mono_ok++;
        }

    printf("compose %s + %s\n", fac_path, anch_path);
    printf("  anchors %d  src %.0fx%.0f  baseline %08x  live %08x\n",
           a.n, a.src_w, a.src_h, a.baseline_crc32, fac_crc);
    printf("  s %.4f..%.4f   max source displacement %.2f px\n", s_lo, s_hi, max_disp);
    printf("  clamped %ld/%d (%.2f%%) cols %d..%d\n", clamped, N * N,
           100.0 * (double)clamped / (double)(N * N),
           clamp_col_max < 0 ? -1 : clamp_col_min, clamp_col_max);
    printf("  monotonic rows %.1f%%   changed %ld entries   span delta %.3f (bound %.3f)\n",
           100.0 * (double)mono_ok / (double)mono_tot, changed, max_span_delta, span_bound);

    /* Gates. Every one refuses; none warns. A composer that warns and writes
     * anyway feeds a boot hook that nobody is watching. */
    if ((double)mono_ok / (double)mono_tot < MIN_MONOTONIC) {
        fprintf(stderr, "REFUSED: composed mesh folds horizontally\n");
        goto done;
    }
    if ((double)clamped / (double)(N * N) > MAX_CLAMP_FRACTION) {
        fprintf(stderr, "REFUSED: %ld control points fall off the sensor\n", clamped);
        goto done;
    }
    if (clamp_col_max >= N - PROTECTED_SEAM_COLS) {
        fprintf(stderr, "REFUSED: clamping reached the seam (column %d >= %d)\n",
                clamp_col_max, N - PROTECTED_SEAM_COLS);
        goto done;
    }
    if (max_span_delta > span_bound) {
        fprintf(stderr, "REFUSED: source span changed by %.2f px (bound %.2f) -- "
                        "that is a scale, not a shear\n", max_span_delta, span_bound);
        goto done;
    }
    {
        FILE *f = fopen(out_path, "wb");
        if (!f) { perror(out_path); goto done; }
        /* Always emit the canonical 2-word header so the file the boot hook
         * feeds to `set` has one shape regardless of how the dump was saved. */
        ((unsigned int *)out)[0] = 0;
        ((unsigned int *)out)[1] = N;
        fwrite(out, 1, BUFSZ, f);
        fclose(f);
        printf("  wrote %s (%d bytes)  crc %08x\n", out_path, BUFSZ,
               crc32_buf((const unsigned char *)nt, (unsigned long)TABLE_BYTES));
    }
    rc = 0;
done:
    free(in);
    free(out);
    return rc;
}

int main(int argc, char **argv)
{
    int fd, rc, id, armed = 0, i, require_baseline = 0;
    const char *cmd, *path;

    /* Line-buffered: this runs over a one-shot socket shell and, when the
     * driver smashed the heap, the abort() took the whole buffered log with it
     * and left "Aborted" as the only evidence. */
    setvbuf(stdout, NULL, _IOLBF, 0);

    if (argc < 2) goto usage;
    cmd = argv[1];

    if (!strcmp(cmd, "compose")) {
        if (argc < 5) goto usage;
        for (i = 5; i < argc; i++)
            if (!strcmp(argv[i], "--require-baseline")) require_baseline = 1;
        return do_compose(argv[2], argv[3], argv[4], require_baseline);
    }

    if (argc < 4) goto usage;
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

usage:
    fprintf(stderr,
            "usage: %s get     <vpe_id> <out.bin>\n"
            "       %s set     <vpe_id> <in.bin> [--i-have-a-recovery-path]\n"
            "       %s compose <factory.bin> <anchors.txt> <out.bin> [--require-baseline]\n",
            argv[0], argv[0], argv[0]);
    return 2;
}
