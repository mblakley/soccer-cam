/* fieldroi -- find the soccer field in one camera frame, on the camera itself,
 * and print the encoder ROI window that covers it.
 *
 * WHY THIS EXISTS. The encoder can be told to favour one rectangle of the
 * picture (docs/ENCODER_ROI_QP.md). The rectangle has to follow the field, and
 * the field sits somewhere different at every ground: across the 2026 games its
 * top row ranges from 110 to 543 and its bottom row from 1283 to 2154
 * (docs/FAR_FIELD_BITS.md). The camera records unattended, so nothing off the
 * camera can be relied on to set it. This runs the project's own field-outline
 * model (training/field_outline, ResNet18, 10 keypoints) with no runtime
 * dependency at all: plain C, static, weights read from a flat file.
 *
 * WHAT IT IS NOT. It is not a general ONNX runtime. The network is hardcoded
 * below and every tensor's shape is checked against the weights file, so a
 * file from a different architecture is refused rather than misread.
 *
 * Keypoint layout (panorama seen from the sideline), from
 * video_grouper/inference/field_detector.py:
 *
 *        9---8---7---6---5      far touchline (top of image)
 *       /                 \
 *      0---1---2---3---4        near touchline (bottom of image)
 *
 * Input:  a baseline JPEG -- what the camera's own snapshot is -- or a binary
 *         PPM (P6, 8-bit RGB), any size. It is resized to 768x384 with the
 *         same bilinear sampling cv2.resize uses in the PC pipeline. A JPEG is
 *         read at one-eighth scale from its DC coefficients, so a 7680x2160
 *         snapshot costs under 1 MB instead of 50.
 * Output: the ten keypoints and scores on stderr, and on stdout a roi.conf
 *         for S37_RoiQp -- or nothing and a non-zero exit if the result does
 *         not look like a field. A window in the wrong place is worse than no
 *         window: it would take bits AWAY from the pitch.
 *
 * usage: fieldroi <weights.bin> <frame.jpg|frame.ppm> [options]
 *   --dump-input F   write the 768x384 picture the network saw, as a PPM
 *   --frame WxH      size of the encoder's frame        (default 7680x2160)
 *   --pad-far N      rows added above the far line      (default 200)
 *   --pad-near N     rows added below the near line     (default 200)
 *   --qp N           QP delta for the window            (default -6)
 *   --bands F,M,N    write three full-width windows instead of one: the far
 *                    22 % of the field's rows (plus the far padding), the
 *                    middle 34 %, and the near 44 % (plus the near padding),
 *                    with QP deltas F, M and N. The far band goes on window 0
 *                    because the encoder gives an overlap to the lower index.
 *                    Needs the ten-window firmware (build 4923 or later);
 *                    S37_RoiQp refuses the file on anything else.
 *   --min-score F    keypoints below this are ignored   (default 0.15)
 *   --min-green F    share of the claimed field rows that must be grass-
 *                    coloured, or the result is refused (default 0.6)
 *   --keypoints      print keypoints only, no roi.conf
 *
 * The far padding is generous on purpose. The model's known error is a far
 * line placed too LOW (measured up to +179 rows on a single frame), which
 * would leave the far touchline outside the window -- the one place this whole
 * effort is for. Padding upward is cheap: that band takes about 4 % of the
 * stream.
 *
 * Build:  gcc -O3 -o fieldroi fieldroi.c -lm
 *         aarch64-linux-gnu-gcc -O3 -static -o fieldroi fieldroi.c -lm
 */
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define IN_W 768
#define IN_H 384
#define NKPT 10

static void die(const char *msg)
{
    fprintf(stderr, "fieldroi: %s\n", msg);
    exit(1);
}

/* --- tensors ---------------------------------------------------------------- */

typedef struct {
    int c, h, w;
    float *d;
} T;

static T talloc(int c, int h, int w)
{
    T t = { c, h, w, malloc(sizeof(float) * (size_t)c * h * w) };
    if (!t.d)
        die("out of memory");
    return t;
}

/* --- weights file ------------------------------------------------------------
 * "FRW1", u32 count, then per tensor: u32 ndim, u32 dims[4], float32 data.
 * Written by export_weights.py in the order the network consumes them. */

static const uint8_t *wp, *wend;

static const float *next_tensor(int d0, int d1, int d2, int d3)
{
    uint32_t hdr[5];
    if (wend - wp < (long)sizeof(hdr))
        die("weights file truncated");
    memcpy(hdr, wp, sizeof(hdr));
    wp += sizeof(hdr);
    int want[4] = { d0, d1, d2, d3 };
    size_t n = 1;
    for (int i = 0; i < 4; i++) {
        if ((int)hdr[1 + i] != want[i]) {
            fprintf(stderr, "fieldroi: weights shape [%u,%u,%u,%u], expected [%d,%d,%d,%d]\n",
                    hdr[1], hdr[2], hdr[3], hdr[4], d0, d1, d2, d3);
            die("weights file does not match this network");
        }
        n *= (size_t)want[i];
    }
    if ((size_t)(wend - wp) < n * sizeof(float))
        die("weights file truncated");
    const float *p = (const float *)wp; /* file is mmap'd page-aligned + 4-byte fields */
    wp += n * sizeof(float);
    return p;
}

/* --- layers ----------------------------------------------------------------- */

/* Direct convolution. The inner loop is a scaled add over one output row, which
 * the compiler vectorises for stride 1. Consumes two tensors from the weights
 * file: W [oc, in.c, k, k] and B [oc]. */
static T conv(T in, int oc, int k, int s, int p, int relu)
{
    const float *W = next_tensor(oc, in.c, k, k);
    const float *B = next_tensor(oc, 1, 1, 1);
    int oh = (in.h + 2 * p - k) / s + 1, ow = (in.w + 2 * p - k) / s + 1;
    T out = talloc(oc, oh, ow);
    for (int o = 0; o < oc; o++) {
        float *op = out.d + (size_t)o * oh * ow;
        for (int j = 0; j < oh * ow; j++)
            op[j] = B[o];
        for (int i = 0; i < in.c; i++) {
            const float *ip = in.d + (size_t)i * in.h * in.w;
            for (int ky = 0; ky < k; ky++) {
                int y0 = ky < p ? (p - ky + s - 1) / s : 0;
                int y1 = (in.h - 1 + p - ky) / s + 1;
                if (y1 > oh)
                    y1 = oh;
                for (int kx = 0; kx < k; kx++) {
                    float w = W[(((size_t)o * in.c + i) * k + ky) * k + kx];
                    int x0 = kx < p ? (p - kx + s - 1) / s : 0;
                    int x1 = (in.w - 1 + p - kx) / s + 1;
                    if (x1 > ow)
                        x1 = ow;
                    for (int y = y0; y < y1; y++) {
                        const float *row = ip + (size_t)(y * s + ky - p) * in.w + (kx - p);
                        float *orow = op + (size_t)y * ow;
                        if (s == 1)
                            for (int x = x0; x < x1; x++)
                                orow[x] += w * row[x];
                        else
                            for (int x = x0; x < x1; x++)
                                orow[x] += w * row[x * s];
                    }
                }
            }
        }
        if (relu)
            for (int j = 0; j < oh * ow; j++)
                if (op[j] < 0)
                    op[j] = 0;
    }
    return out;
}

/* 3x3, stride 2, pad 1. Padding never wins a max, so it is simply skipped. */
static T maxpool(T in)
{
    int oh = (in.h + 2 - 3) / 2 + 1, ow = (in.w + 2 - 3) / 2 + 1;
    T out = talloc(in.c, oh, ow);
    for (int c = 0; c < in.c; c++) {
        const float *ip = in.d + (size_t)c * in.h * in.w;
        float *op = out.d + (size_t)c * oh * ow;
        for (int y = 0; y < oh; y++)
            for (int x = 0; x < ow; x++) {
                float m = -INFINITY;
                for (int ky = 0; ky < 3; ky++) {
                    int yy = y * 2 + ky - 1;
                    if (yy < 0 || yy >= in.h)
                        continue;
                    for (int kx = 0; kx < 3; kx++) {
                        int xx = x * 2 + kx - 1;
                        if (xx < 0 || xx >= in.w)
                            continue;
                        float v = ip[(size_t)yy * in.w + xx];
                        if (v > m)
                            m = v;
                    }
                }
                op[(size_t)y * ow + x] = m;
            }
    }
    return out;
}

/* ResNet basic block. Weight order in the file is the ONNX graph's: conv1,
 * conv2, then the 1x1 downsample when the block changes shape. Frees `in`. */
static T block(T in, int oc, int stride)
{
    T a = conv(in, oc, 3, stride, 1, 1);
    T b = conv(a, oc, 3, 1, 1, 0);
    free(a.d);
    T skip = in;
    if (stride != 1 || in.c != oc) {
        skip = conv(in, oc, 1, stride, 0, 0);
        free(in.d);
    }
    size_t n = (size_t)b.c * b.h * b.w;
    for (size_t j = 0; j < n; j++) {
        float v = b.d[j] + skip.d[j];
        b.d[j] = v > 0 ? v : 0;
    }
    free(skip.d);
    return b;
}

/* y = W x + b, W [out, in] row-major (ONNX Gemm with transB=1). */
static void linear(const float *x, int nin, float *y, int nout)
{
    const float *W = next_tensor(nout, nin, 1, 1);
    const float *B = next_tensor(nout, 1, 1, 1);
    for (int o = 0; o < nout; o++) {
        float acc = B[o];
        for (int i = 0; i < nin; i++)
            acc += W[(size_t)o * nin + i] * x[i];
        y[o] = acc;
    }
}

static float sigmoidf(float v)
{
    return 1.0f / (1.0f + expf(-v));
}

/* rgb: IN_H x IN_W x 3, 8-bit. kpts: x,y pairs in 768x384 pixel space. */
static void infer(const uint8_t *rgb, float kpts[NKPT][2], float scores[NKPT])
{
    const float *mean = next_tensor(3, 1, 1, 1);
    const float *std = next_tensor(3, 1, 1, 1);
    T x = talloc(3, IN_H, IN_W);
    for (int c = 0; c < 3; c++)
        for (int j = 0; j < IN_H * IN_W; j++)
            x.d[(size_t)c * IN_H * IN_W + j] = (rgb[(size_t)j * 3 + c] / 255.0f - mean[c]) / std[c];

    T t = conv(x, 64, 7, 2, 3, 1);
    free(x.d);
    T u = maxpool(t);
    free(t.d);
    static const int widths[4] = { 64, 128, 256, 512 };
    for (int stage = 0; stage < 4; stage++) {
        u = block(u, widths[stage], stage == 0 ? 1 : 2);
        u = block(u, widths[stage], 1);
    }

    float feat[512], trunk[256], kraw[NKPT * 2], sraw[NKPT];
    size_t plane = (size_t)u.h * u.w;
    for (int c = 0; c < 512; c++) {
        double acc = 0;
        for (size_t j = 0; j < plane; j++)
            acc += u.d[(size_t)c * plane + j];
        feat[c] = (float)(acc / (double)plane);
    }
    free(u.d);
    linear(feat, 512, trunk, 256);
    for (int i = 0; i < 256; i++)
        if (trunk[i] < 0)
            trunk[i] = 0;
    /* Both heads read the same trunk; file order is kpt head, then score head. */
    linear(trunk, 256, kraw, NKPT * 2);
    linear(trunk, 256, sraw, NKPT);
    for (int k = 0; k < NKPT; k++) {
        kpts[k][0] = sigmoidf(kraw[k * 2]) * IN_W;
        kpts[k][1] = sigmoidf(kraw[k * 2 + 1]) * IN_H;
        scores[k] = sigmoidf(sraw[k]);
    }
    if (wp != wend)
        die("weights file has data left over; it does not match this network");
}

/* --- image ------------------------------------------------------------------ */

static int ppm_int(FILE *f)
{
    int c, v = 0, any = 0;
    for (;;) {
        c = fgetc(f);
        if (c == '#') {
            while (c != '\n' && c != EOF)
                c = fgetc(f);
            continue;
        }
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            if (any)
                return v;
            continue;
        }
        if (c < '0' || c > '9')
            die("malformed PPM header");
        v = v * 10 + (c - '0');
        any = 1;
        if (v > 100000)
            die("PPM dimension too large");
    }
}

/* Load a P6 PPM and resize to IN_W x IN_H. Bilinear with half-pixel centres and
 * no antialiasing, i.e. what cv2.resize(..., INTER_LINEAR) does in the PC
 * pipeline the model was trained through. Reads the source one row pair at a
 * time, so a 7680x2160 frame never needs 50 MB of RAM. */
static uint8_t *resize_ppm(FILE *f)
{
    if (fgetc(f) != 'P' || fgetc(f) != '6')
        die("frame is not a binary PPM (P6)");
    int w = ppm_int(f), h = ppm_int(f), maxv = ppm_int(f);
    if (w < 2 || h < 2 || maxv != 255)
        die("PPM must be 8-bit RGB");
    long data = ftell(f);
    uint8_t *out = malloc((size_t)IN_W * IN_H * 3);
    uint8_t *r0 = malloc((size_t)w * 3), *r1 = malloc((size_t)w * 3);
    if (!out || !r0 || !r1)
        die("out of memory");
    for (int y = 0; y < IN_H; y++) {
        double sy = (y + 0.5) * h / IN_H - 0.5;
        if (sy < 0)
            sy = 0;
        int y0 = (int)sy, y1 = y0 + 1 < h ? y0 + 1 : y0;
        double fy = sy - y0;
        if (fseek(f, data + (long)y0 * w * 3, SEEK_SET) || fread(r0, 3, (size_t)w, f) != (size_t)w)
            die("PPM is shorter than its header says");
        if (fseek(f, data + (long)y1 * w * 3, SEEK_SET) || fread(r1, 3, (size_t)w, f) != (size_t)w)
            die("PPM is shorter than its header says");
        for (int x = 0; x < IN_W; x++) {
            double sx = (x + 0.5) * w / IN_W - 0.5;
            if (sx < 0)
                sx = 0;
            int x0 = (int)sx, x1 = x0 + 1 < w ? x0 + 1 : x0;
            double fx = sx - x0;
            for (int c = 0; c < 3; c++) {
                double top = r0[x0 * 3 + c] * (1 - fx) + r0[x1 * 3 + c] * fx;
                double bot = r1[x0 * 3 + c] * (1 - fx) + r1[x1 * 3 + c] * fx;
                out[((size_t)y * IN_W + x) * 3 + c] = (uint8_t)(top * (1 - fy) + bot * fy + 0.5);
            }
        }
    }
    free(r0);
    free(r1);
    fclose(f);
    return out;
}

/* --- JPEG, DC coefficients only ---------------------------------------------
 * The camera's snapshot (cmd=Snap) is a 7680x2160 baseline JPEG. Decoding it
 * properly needs 50 MB for the pixels, on a camera whose whole Linux has about
 * 256 MB and is busy recording. The network only wants 768x384, so this reads
 * just the DC coefficient of every 8x8 block -- the block's mean -- which is
 * the picture at one-eighth scale (960x270) for under 1 MB. The AC
 * coefficients still have to be Huffman-decoded to find where each block ends,
 * but they are thrown away. Measured across 27 games, feeding the network this
 * instead of the full-resolution frame moves the far row by a median of 1 row
 * (worst 95) and the near row by 15 (worst 141), almost always outward.
 *
 * Baseline / extended-sequential Huffman only, 8-bit, one interleaved scan --
 * what the camera writes. Anything else is refused. */

typedef struct {
    const uint8_t *p, *end;
    unsigned acc;
    int nbits, marker, starved;
} BitR;

static int br_bit(BitR *b)
{
    if (b->nbits == 0) {
        unsigned c = 0;
        if (b->marker || b->p >= b->end)
            b->starved = 1; /* asked for bits the file does not have */
        if (!b->marker && b->p < b->end) {
            c = *b->p++;
            if (c == 0xFF) {
                unsigned n = b->p < b->end ? *b->p : 0xD9;
                if (n == 0) {
                    b->p++; /* FF 00 is a stuffed FF */
                } else {
                    b->marker = (int)n; /* a real marker: leave it, feed zeros */
                    b->p--;
                    c = 0;
                }
            }
        }
        b->acc = c;
        b->nbits = 8;
    }
    b->nbits--;
    return (int)(b->acc >> b->nbits) & 1;
}

static int br_bits(BitR *b, int n)
{
    int v = 0;
    while (n-- > 0)
        v = (v << 1) | br_bit(b);
    return v;
}

typedef struct {
    int mincode[17], maxcode[17], valptr[17], set;
    uint8_t vals[256];
} Huff;

static int huff_decode(BitR *b, const Huff *h)
{
    int code = 0;
    for (int l = 1; l <= 16; l++) {
        code = (code << 1) | br_bit(b);
        if (h->maxcode[l] >= 0 && code >= h->mincode[l] && code <= h->maxcode[l])
            return h->vals[h->valptr[l] + code - h->mincode[l]];
    }
    die("corrupt JPEG: bad Huffman code");
    return 0;
}

static unsigned be16(const uint8_t *p)
{
    return ((unsigned)p[0] << 8) | p[1];
}

/* Returns a malloc'd binary PPM of the 1/8-scale picture; *out_len is its size. */
static uint8_t *jpeg_dc_to_ppm(const uint8_t *d, size_t n, size_t *out_len)
{
    int qdc[4] = { 0, 0, 0, 0 }, w = 0, h = 0, ncomp = 0, ri = 0;
    int cid[3], ch[3], cv[3], ctq[3], ctd[3] = { 0, 0, 0 }, cta[3] = { 0, 0, 0 };
    static Huff dc_t[4], ac_t[4];
    size_t i = 2, scan = 0;

    while (!scan) {
        if (i + 4 > n || d[i] != 0xFF)
            die("corrupt JPEG: marker expected");
        unsigned m = d[i + 1];
        if (m == 0xFF) { /* fill byte */
            i++;
            continue;
        }
        size_t len = be16(d + i + 2);
        if (len < 2 || i + 2 + len > n)
            die("corrupt JPEG: segment runs past the end");
        const uint8_t *s = d + i + 4, *e = d + i + 2 + len;
        if (m == 0xDB) {
            while (s < e) {
                int pq = s[0] >> 4, tq = s[0] & 15;
                size_t sz = pq ? 128 : 64;
                if (tq > 3 || s + 1 + sz > e)
                    die("corrupt JPEG: quantisation table");
                qdc[tq] = pq ? (int)be16(s + 1) : s[1]; /* entry 0 is the DC step */
                s += 1 + sz;
            }
        } else if (m == 0xC0 || m == 0xC1) {
            if (e - s < 6 || s[0] != 8)
                die("JPEG is not 8-bit");
            h = (int)be16(s + 1);
            w = (int)be16(s + 3);
            ncomp = s[5];
            if ((ncomp != 1 && ncomp != 3) || e - s < 6 + 3 * ncomp || w < 8 || h < 8)
                die("JPEG has an unsupported layout");
            for (int c = 0; c < ncomp; c++) {
                cid[c] = s[6 + 3 * c];
                ch[c] = s[7 + 3 * c] >> 4;
                cv[c] = s[7 + 3 * c] & 15;
                ctq[c] = s[8 + 3 * c] & 3;
                if (ch[c] < 1 || ch[c] > 2 || cv[c] < 1 || cv[c] > 2)
                    die("JPEG has unsupported chroma subsampling");
            }
        } else if (m == 0xC2 || (m >= 0xC5 && m <= 0xCF && m != 0xC8 && m != 0xCC)) {
            die("JPEG is progressive or arithmetic-coded; only baseline is supported");
        } else if (m == 0xC4) {
            while (s < e) {
                int tc = s[0] >> 4, th = s[0] & 15, total = 0;
                if (tc > 1 || th > 3 || s + 17 > e)
                    die("corrupt JPEG: Huffman table");
                Huff *t = tc ? &ac_t[th] : &dc_t[th];
                int code = 0;
                for (int l = 1; l <= 16; l++) {
                    int cnt = s[l];
                    t->valptr[l] = total;
                    t->mincode[l] = code;
                    t->maxcode[l] = cnt ? code + cnt - 1 : -1;
                    code = (code + cnt) << 1;
                    total += cnt;
                }
                if (total > 256 || s + 17 + total > e)
                    die("corrupt JPEG: Huffman table");
                memcpy(t->vals, s + 17, (size_t)total);
                t->set = 1;
                s += 17 + total;
            }
        } else if (m == 0xDD) {
            if (e - s < 2)
                die("corrupt JPEG: restart interval");
            ri = (int)be16(s);
        } else if (m == 0xDA) {
            if (!ncomp || e - s < 1 + 2 * ncomp + 3 || s[0] != ncomp)
                die("JPEG is not a single interleaved scan");
            for (int c = 0; c < ncomp; c++) {
                if (s[1 + 2 * c] != cid[c])
                    die("JPEG scan lists components out of order");
                ctd[c] = s[2 + 2 * c] >> 4;
                cta[c] = s[2 + 2 * c] & 15;
                if (ctd[c] > 3 || cta[c] > 3 || !dc_t[ctd[c]].set || !ac_t[cta[c]].set || !qdc[ctq[c]])
                    die("JPEG scan uses a table it never defined");
            }
            scan = i + 2 + len;
        }
        i += 2 + len;
    }

    int hmax = 1, vmax = 1;
    for (int c = 0; c < ncomp; c++) {
        if (ch[c] > hmax)
            hmax = ch[c];
        if (cv[c] > vmax)
            vmax = cv[c];
    }
    int mcux = (w + 8 * hmax - 1) / (8 * hmax), mcuy = (h + 8 * vmax - 1) / (8 * vmax);
    uint8_t *plane[3] = { 0, 0, 0 };
    for (int c = 0; c < ncomp; c++) {
        plane[c] = malloc((size_t)mcux * ch[c] * mcuy * cv[c]);
        if (!plane[c])
            die("out of memory");
    }

    BitR br = { d + scan, d + n, 0, 0, 0, 0 };
    int pred[3] = { 0, 0, 0 };
    long mcu = 0;
    for (int my = 0; my < mcuy; my++)
        for (int mx = 0; mx < mcux; mx++, mcu++) {
            if (ri && mcu && mcu % ri == 0) {
                /* Restart: drop the padding bits, step over the RSTn marker. */
                br.nbits = 0;
                if (br.p + 2 > br.end || br.p[0] != 0xFF || (br.p[1] & 0xF8) != 0xD0)
                    die("corrupt JPEG: restart marker missing");
                br.p += 2;
                br.marker = 0;
                pred[0] = pred[1] = pred[2] = 0;
            }
            for (int c = 0; c < ncomp; c++)
                for (int by = 0; by < cv[c]; by++)
                    for (int bx = 0; bx < ch[c]; bx++) {
                        int s = huff_decode(&br, &dc_t[ctd[c]]);
                        if (s > 11)
                            die("corrupt JPEG: DC size");
                        int diff = br_bits(&br, s);
                        if (s && diff < (1 << (s - 1)))
                            diff -= (1 << s) - 1;
                        pred[c] += diff;
                        for (int k = 1; k < 64;) { /* walk the AC run to find the block's end */
                            int rs = huff_decode(&br, &ac_t[cta[c]]);
                            int r = rs >> 4, sz = rs & 15;
                            if (!sz) {
                                if (r != 15)
                                    break; /* end of block */
                                k += 16;
                            } else {
                                k += r + 1;
                                (void)br_bits(&br, sz);
                            }
                        }
                        /* The DC term contributes coefficient/8 to every sample. */
                        int v = (pred[c] * qdc[ctq[c]]) / 8 + 128;
                        v = v < 0 ? 0 : v > 255 ? 255 : v;
                        plane[c][(size_t)(my * cv[c] + by) * (mcux * ch[c]) + mx * ch[c] + bx] = (uint8_t)v;
                    }
        }

    /* A snapshot cut short still Huffman-decodes: the reader feeds zeros past
     * the end and the lower part of the picture comes out as flat garbage. The
     * network would then be shown half a field and answer anyway. */
    if (br.starved)
        die("JPEG is truncated: the picture data ends before the last block");

    int ow = (w + 7) / 8, oh = (h + 7) / 8;
    char hdr[48];
    int hl = snprintf(hdr, sizeof(hdr), "P6\n%d %d\n255\n", ow, oh);
    uint8_t *out = malloc((size_t)hl + (size_t)ow * oh * 3);
    if (!out)
        die("out of memory");
    memcpy(out, hdr, (size_t)hl);
    uint8_t *px = out + hl;
    for (int y = 0; y < oh; y++)
        for (int x = 0; x < ow; x++, px += 3) {
            int Y = plane[0][(size_t)(y * cv[0] / vmax) * (mcux * ch[0]) + x * ch[0] / hmax];
            if (ncomp == 1) {
                px[0] = px[1] = px[2] = (uint8_t)Y;
                continue;
            }
            int cb = plane[1][(size_t)(y * cv[1] / vmax) * (mcux * ch[1]) + x * ch[1] / hmax] - 128;
            int cr = plane[2][(size_t)(y * cv[2] / vmax) * (mcux * ch[2]) + x * ch[2] / hmax] - 128;
            int r = Y + (int)lround(1.402 * cr);
            int g = Y - (int)lround(0.344136 * cb + 0.714136 * cr);
            int b = Y + (int)lround(1.772 * cb);
            px[0] = (uint8_t)(r < 0 ? 0 : r > 255 ? 255 : r);
            px[1] = (uint8_t)(g < 0 ? 0 : g > 255 ? 255 : g);
            px[2] = (uint8_t)(b < 0 ? 0 : b > 255 ? 255 : b);
        }
    for (int c = 0; c < ncomp; c++)
        free(plane[c]);
    *out_len = (size_t)hl + (size_t)ow * oh * 3;
    return out;
}

/* A frame is a PPM (streamed from disk row by row) or a baseline JPEG (read
 * whole, a few MB at most, and reduced to its DC picture). Told apart by the
 * first two bytes, not the file name. */
static uint8_t *load_frame(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        die("cannot open the frame");
    int a = fgetc(f), b = fgetc(f);
    rewind(f);
    if (a == 'P' && b == '6')
        return resize_ppm(f);
    if (a != 0xFF || b != 0xD8)
        die("frame is neither a binary PPM (P6) nor a JPEG");
    if (fseek(f, 0, SEEK_END))
        die("cannot read the frame");
    long len = ftell(f);
    rewind(f);
    if (len < 16 || len > 64L * 1024 * 1024)
        die("JPEG is empty or implausibly large");
    uint8_t *raw = malloc((size_t)len);
    if (!raw || fread(raw, 1, (size_t)len, f) != (size_t)len)
        die("cannot read the frame");
    fclose(f);
    size_t ppm_len = 0;
    uint8_t *ppm = jpeg_dc_to_ppm(raw, (size_t)len, &ppm_len);
    free(raw);
    FILE *m = fmemopen(ppm, ppm_len, "rb");
    if (!m)
        die("out of memory");
    uint8_t *out = resize_ppm(m); /* closes m */
    free(ppm);
    return out;
}

/* --- main ------------------------------------------------------------------- */

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: fieldroi <weights.bin> <frame.ppm> [--frame WxH] [--pad-far N]\n"
                        "                [--pad-near N] [--qp N] [--min-score F] [--min-green F]\n"
                        "                [--bands F,M,N] [--keypoints]\n");
        return 2;
    }
    int fw = 7680, fh = 2160, pad_far = 200, pad_near = 200, qp = -6, only_kpts = 0;
    int bands = 0, band_qp[3] = {0, 0, 0};
    double min_score = 0.15, min_green = 0.6;
    const char *dump_input = NULL;
    for (int i = 3; i < argc; i++) {
        if (!strcmp(argv[i], "--keypoints"))
            only_kpts = 1;
        else if (i + 1 >= argc)
            die("option needs a value");
        else if (!strcmp(argv[i], "--frame")) {
            if (sscanf(argv[++i], "%dx%d", &fw, &fh) != 2)
                die("--frame wants WxH");
        } else if (!strcmp(argv[i], "--pad-far"))
            pad_far = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--pad-near"))
            pad_near = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--qp"))
            qp = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--bands")) {
            char tail;
            if (sscanf(argv[++i], "%d,%d,%d%c", &band_qp[0], &band_qp[1], &band_qp[2], &tail) != 3)
                die("--bands wants three QP deltas: F,M,N");
            bands = 1;
        } else if (!strcmp(argv[i], "--min-score"))
            min_score = atof(argv[++i]);
        else if (!strcmp(argv[i], "--min-green"))
            min_green = atof(argv[++i]);
        else if (!strcmp(argv[i], "--dump-input"))
            dump_input = argv[++i];
        else
            die("unknown option");
    }
    if (fw < 16 || fh < 16 || fw > 65535 || fh > 65535 || pad_far < 0 || pad_near < 0 || qp < -32 || qp > 31)
        die("option out of range");
    for (int b = 0; b < 3; b++)
        if (band_qp[b] < -32 || band_qp[b] > 31)
            die("--bands: a QP delta is outside -32..31");

    int fd = open(argv[1], O_RDONLY);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || st.st_size < 8)
        die("cannot open the weights file");
    /* Read-only and file-backed: the kernel can drop these pages under memory
     * pressure instead of this process pushing the camera's app into OOM. */
    const uint8_t *base = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (base == MAP_FAILED)
        die("cannot map the weights file");
    if (memcmp(base, "FRW1", 4))
        die("not a fieldroi weights file");
    wp = base + 8;
    wend = base + st.st_size;

    uint8_t *rgb = load_frame(argv[2]);
    if (dump_input) {
        /* What the network was actually shown, for "why did it say that". */
        FILE *o = fopen(dump_input, "wb");
        if (!o || fprintf(o, "P6\n%d %d\n255\n", IN_W, IN_H) < 0 ||
            fwrite(rgb, 3, (size_t)IN_W * IN_H, o) != (size_t)IN_W * IN_H || fclose(o))
            die("cannot write --dump-input");
    }
    float kpts[NKPT][2], scores[NKPT];
    infer(rgb, kpts, scores);

    for (int k = 0; k < NKPT; k++)
        fprintf(stderr, "kpt %d  x %8.3f  y %8.3f  score %.4f\n", k, kpts[k][0], kpts[k][1], scores[k]);
    if (only_kpts) {
        free(rgb);
        return 0;
    }

    /* Rows only. The field spans nearly the full width at every ground, so the
     * window is full-width and two numbers decide it. */
    double far_y = 1e9, near_y = -1e9;
    int n_far = 0, n_near = 0;
    for (int k = 0; k < NKPT; k++) {
        if (scores[k] < min_score)
            continue;
        double y = kpts[k][1] * fh / IN_H;
        if (k >= 5) {
            n_far++;
            if (y < far_y)
                far_y = y;
        } else {
            n_near++;
            if (y > near_y)
                near_y = y;
        }
    }
    /* Fail closed. Each of these means "this is not a field outline". */
    if (n_far < 3 || n_near < 3) {
        fprintf(stderr, "fieldroi: REFUSED: only %d far and %d near keypoints clear score %.2f\n", n_far, n_near,
                min_score);
        return 3;
    }
    if (near_y - far_y < fh * 0.25) {
        fprintf(stderr, "fieldroi: REFUSED: field would be %.0f rows tall, under a quarter of the frame\n",
                near_y - far_y);
        return 3;
    }
    /* The network always answers. Shown a uniformly grey or black frame it
     * still returns a tidy outline with ordinary scores (measured), so the
     * scores cannot tell a field from a lens cap. Ask the picture instead: the
     * rows it claims are pitch must actually be mostly green. Centre 70 % of
     * the width, because the pitch's corners fall away at the edges. */
    {
        int ya = (int)(far_y * IN_H / fh), yb = (int)(near_y * IN_H / fh);
        ya = ya < 0 ? 0 : ya;
        yb = yb > IN_H ? IN_H : yb;
        long green = 0, total = 0;
        for (int y = ya; y < yb; y++)
            for (int x = IN_W * 15 / 100; x < IN_W * 85 / 100; x++) {
                const uint8_t *px = rgb + ((size_t)y * IN_W + x) * 3;
                int r = px[0], g = px[1], b = px[2];
                /* "Grass" is yellow-green through green: sunlit turf has
                 * red about equal to green (measured mean RGB 141,135,66 on
                 * one game), so the test is that blue is clearly lowest and
                 * red does not dominate. On four real games this passes 87 to
                 * 93 % of the field rows; a grey or black frame scores 0. */
                green += g * 20 >= r * 17 && g * 10 >= b * 11 && g - b >= 12;
                total++;
            }
        double frac = total ? (double)green / (double)total : 0.0;
        fprintf(stderr, "green fraction inside the claimed field rows: %.2f\n", frac);
        if (frac < min_green) {
            fprintf(stderr, "fieldroi: REFUSED: only %.0f %% of the claimed field is green (need %.0f %%)\n",
                    frac * 100, min_green * 100);
            return 3;
        }
    }
    free(rgb);
    int top = (int)floor(far_y) - pad_far, bot = (int)ceil(near_y) + pad_near;
    top = top < 0 ? 0 : top / 16 * 16;                 /* outward to the 16-pixel QP grid */
    bot = bot > fh ? fh : (bot + 15) / 16 * 16;
    if (bot > fh)
        bot = fh;
    fprintf(stderr, "field rows %.0f..%.0f  ->  window rows %d..%d (%d far, %d near keypoints)\n", far_y, near_y, top,
            bot, n_far, n_near);

    if (bands) {
        /* Three bands that tile [top, bot) exactly. The cuts sit at 22 % and
         * 56 % of the field's own height (not the padded window's), on the
         * 16-pixel grid: on the frame the far third of a pitch is only about a
         * fifth of its rows, and that is where a ball is smallest. */
        double depth = near_y - far_y;
        int cut1 = (int)(far_y + 0.22 * depth) / 16 * 16, cut2 = (int)(far_y + 0.56 * depth) / 16 * 16;
        if (cut1 - top < 16 || cut2 - cut1 < 16 || bot - cut2 < 16) {
            fprintf(stderr, "fieldroi: REFUSED: bands %d..%d..%d..%d leave one under 16 rows\n", top, cut1, cut2, bot);
            return 3;
        }
        fprintf(stderr, "bands: far %d..%d (%d), mid %d..%d (%d), near %d..%d (%d)\n", top, cut1, band_qp[0], cut1, cut2,
                band_qp[1], cut2, bot, band_qp[2]);
        printf("# written by fieldroi: field rows %.0f..%.0f, padded %d above / %d below, three bands\n", far_y, near_y,
               pad_far, pad_near);
        printf("enable=1\npath=0\nmode=1\nframe_w=%d\nframe_h=%d\n", fw, fh);
        printf("win0=0,%d,%d,%d,%d\nwin1=0,%d,%d,%d,%d\nwin2=0,%d,%d,%d,%d\n", top, fw, cut1 - top, band_qp[0], cut1, fw,
               cut2 - cut1, band_qp[1], cut2, fw, bot - cut2, band_qp[2]);
        return 0;
    }
    printf("# written by fieldroi: field rows %.0f..%.0f, padded %d above / %d below\n", far_y, near_y, pad_far,
           pad_near);
    printf("enable=1\npath=0\nx=0\ny=%d\nw=%d\nh=%d\nqp=%d\nmode=1\nframe_w=%d\nframe_h=%d\n", top, fw, bot - top, qp,
           fw, fh);
    return 0;
}
