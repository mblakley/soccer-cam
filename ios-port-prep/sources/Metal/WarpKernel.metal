// Metal/WarpKernel.metal
//
// Warp-once-crop compute kernel: sample the constant leveling panorama (Lx,
// Ly) at a per-frame crop box, then bilinear-sample the source. Port of the
// OpenCL kernel in video_grouper/inference/opencl_warp.py.
//
// Parity gate (E0.B3): per-pixel diff vs cv2.remap baseline within < 1 LSB
// mean uint8 and < 5 LSB max. The float math + half-pixel offset + bilinear
// formulation must match cv2.remap exactly.

#include <metal_stdlib>
using namespace metal;

inline float bilL(device const float* L, int pw, int ph, float px, float py) {
    int x0 = int(floor(px));
    int y0 = int(floor(py));
    float ax = px - float(x0);
    float ay = py - float(y0);
    x0 = clamp(x0, 0, pw - 2);
    y0 = clamp(y0, 0, ph - 2);
    float a = L[y0 * pw + x0];
    float b = L[y0 * pw + x0 + 1];
    float c = L[(y0 + 1) * pw + x0];
    float d = L[(y0 + 1) * pw + x0 + 1];
    return (a * (1.0f - ax) + b * ax) * (1.0f - ay)
         + (c * (1.0f - ax) + d * ax) * ay;
}

struct WarpParams {
    int sw;
    int sh;
    int pw;
    int ph;
    int cx;
    int cy;
    int cw;
    int ch;
    int ow;
    int oh;
};

kernel void warp(
    device const uchar4*       src    [[buffer(0)]],
    device const float*        Lx     [[buffer(1)]],
    device const float*        Ly     [[buffer(2)]],
    constant WarpParams&       params [[buffer(3)]],
    device uchar4*             dst    [[buffer(4)]],
    uint2                      gid    [[thread_position_in_grid]]
) {
    int x = int(gid.x);
    int y = int(gid.y);
    if (x >= params.ow || y >= params.oh) return;
    int o = y * params.ow + x;

    // Half-pixel offset matches cv2.resize / cv2.remap conventions exactly.
    // Removing the -0.5f drifts the output by half a pixel.
    float px = float(params.cx)
        + (float(x) + 0.5f) * float(params.cw) / float(params.ow) - 0.5f;
    float py = float(params.cy)
        + (float(y) + 0.5f) * float(params.ch) / float(params.oh) - 0.5f;
    float sx = bilL(Lx, params.pw, params.ph, px, py);
    float sy = bilL(Ly, params.pw, params.ph, px, py);

    int x0 = int(floor(sx));
    int y0 = int(floor(sy));
    float ax = sx - float(x0);
    float ay = sy - float(y0);

    uchar4 result = uchar4(0, 0, 0, 255);
    if (x0 >= 0 && y0 >= 0 && x0 + 1 < params.sw && y0 + 1 < params.sh) {
        uchar4 p00 = src[y0 * params.sw + x0];
        uchar4 p01 = src[y0 * params.sw + x0 + 1];
        uchar4 p10 = src[(y0 + 1) * params.sw + x0];
        uchar4 p11 = src[(y0 + 1) * params.sw + x0 + 1];

        // Per-channel bilinear, kept in this exact factored form to match
        // cv2.remap's INTER_LINEAR rounding. Don't refactor the parens.
        float4 f00 = float4(p00);
        float4 f01 = float4(p01);
        float4 f10 = float4(p10);
        float4 f11 = float4(p11);
        float4 v = (f00 * (1.0f - ax) + f01 * ax) * (1.0f - ay)
                 + (f10 * (1.0f - ax) + f11 * ax) * ay;
        // The OpenCL reference does (uchar)(v + 0.5f) — round-to-nearest from
        // non-negative float. Metal: clamp + cast achieves the same.
        result = uchar4(clamp(v + 0.5f, 0.0f, 255.0f));
        result.a = 255;
    }
    dst[o] = result;
}
