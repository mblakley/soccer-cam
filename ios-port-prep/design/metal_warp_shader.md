# Metal warp shader — kernel spec

Port of `video_grouper/inference/opencl_warp.py` (OpenCL kernel + host code)
to Metal. The kernel does the same warp-once-crop math the OpenCL backend
does: bilinear-sample the constant leveling-pano maps `Lx`/`Ly` at the
per-frame crop box, then bilinear-sample the source.

**Parity target:** Metal-shader output vs `cv2.remap` (the CPU reference)
differs by < 1 LSB mean uint8, < 5 LSB max (single-pixel peaks allowed for
bilinear float-precision differences). E0.B3.

## Files

```
Metal/
├── WarpKernel.metal         # the compute shader
└── MetalDevice.swift        # shared MTLDevice, command queue, library
Pipeline/
└── MetalWarpRenderer.swift  # host wrapper — uploads pano once, dispatches per frame
```

## `Metal/WarpKernel.metal`

```metal
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
    int sw;          // source width
    int sh;          // source height
    int pw;          // pano width
    int ph;          // pano height
    int cx;          // crop origin x
    int cy;          // crop origin y
    int cw;          // crop width
    int ch;          // crop height
    int ow;          // output width
    int oh;          // output height
};

kernel void warp(
    device const uchar4*       src    [[buffer(0)]],   // BGRA8 source (Apple's CVPixelBuffer format)
    device const float*        Lx     [[buffer(1)]],
    device const float*        Ly     [[buffer(2)]],
    constant WarpParams&       params [[buffer(3)]],
    device uchar4*             dst    [[buffer(4)]],   // BGRA8 output
    uint2                      gid    [[thread_position_in_grid]]
) {
    int x = int(gid.x), y = int(gid.y);
    if (x >= params.ow || y >= params.oh) return;
    int o = y * params.ow + x;

    // half-pixel convention matches cv2.resize / cv2.remap
    float px = float(params.cx) + (float(x) + 0.5f) * float(params.cw) / float(params.ow) - 0.5f;
    float py = float(params.cy) + (float(y) + 0.5f) * float(params.ch) / float(params.oh) - 0.5f;
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

        // Per-channel bilinear. Match the OpenCL kernel's uchar cast (+0.5 round).
        float4 f00 = float4(p00);
        float4 f01 = float4(p01);
        float4 f10 = float4(p10);
        float4 f11 = float4(p11);
        float4 v = (f00 * (1.0f - ax) + f01 * ax) * (1.0f - ay)
                 + (f10 * (1.0f - ax) + f11 * ax) * ay;
        // The OpenCL ref does v + 0.5 → uchar (round to nearest). Match exactly.
        result = uchar4(clamp(v + 0.5f, 0.0f, 255.0f));
        result.a = 255;
    }
    dst[o] = result;
}
```

### Key parity rules

- **half-pixel offset** — `(x + 0.5) * cw / ow - 0.5` matches `cv2.resize` /
  `cv2.remap`. The OpenCL reference uses this; the Metal kernel must too,
  or the output drifts by half a pixel.
- **bilinear formulation** — `(p00*(1-ax) + p01*ax)*(1-ay) + (p10*(1-ax) + p11*ax)*ay`
  with the multiplications inside the parentheses, NOT factored. This
  is the order `cv2.remap` uses for `INTER_LINEAR`; reordering produces
  ULP-level drift that fails the E0.B3 bound on edge pixels.
- **uchar cast** — `(uchar)(v + 0.5f)` is the OpenCL spec for round-to-
  nearest from a non-negative float. Metal's `uchar4(clamp(v + 0.5, 0, 255))`
  matches because the clamp guarantees non-negative.
- **out-of-bounds → 0** — matches `cv2.remap`'s `BORDER_CONSTANT, borderValue=0`.

### Pixel-format choice — BGRA8

`CVPixelBuffer`s from `AVAssetReader` come back most cheaply as
`kCVPixelFormatType_32BGRA`. Using `uchar4` BGRA in the kernel means no
shuffle. The output `CVPixelBuffer` (also BGRA) gets handed straight to
VideoToolbox H.264 encoder which can accept BGRA directly.

If `AVAssetReader` requests RGB24 to match the Python `frame.to_ndarray(format="rgb24")`,
that's a `uchar3` pack — Metal's threadgroup memory loads are happier with
4-byte alignment, hence BGRA. The Python parity reference is RGB24; we
convert when comparing PNG samples (cheap; only on the parity test path).

## `Pipeline/MetalWarpRenderer.swift`

```swift
import Metal
import MetalKit
import CoreVideo

public final class MetalWarpRenderer {
    private let device: MTLDevice
    private let commandQueue: MTLCommandQueue
    private let warpPipeline: MTLComputePipelineState

    // Constant per game — uploaded once.
    private let lx: MTLBuffer
    private let ly: MTLBuffer
    private let panoWidth: Int
    private let panoHeight: Int

    public init(leveledPano: LeveledPano) throws {
        let device = MetalDevice.shared.device
        let library = MetalDevice.shared.library
        let fn = library.makeFunction(name: "warp")!
        self.device = device
        self.commandQueue = device.makeCommandQueue()!
        self.warpPipeline = try device.makeComputePipelineState(function: fn)

        // Upload the constant pano maps ONCE.
        let panoFloats = leveledPano.mapX.count * MemoryLayout<Float>.stride
        self.lx = device.makeBuffer(
            bytes: leveledPano.mapX, length: panoFloats, options: [.storageModeShared]
        )!
        self.ly = device.makeBuffer(
            bytes: leveledPano.mapY, length: panoFloats, options: [.storageModeShared]
        )!
        self.panoWidth = leveledPano.width
        self.panoHeight = leveledPano.height
    }

    /// Warp one frame given a per-frame crop box. Source and dst are
    /// CVPixelBuffer-backed MTLTextures so the round-trip with
    /// AVAssetReader / VideoToolbox stays zero-copy.
    public func warp(
        source: CVPixelBuffer,
        destination: CVPixelBuffer,
        cropBox: (x: Int, y: Int, w: Int, h: Int)
    ) throws {
        let srcBuffer = try MetalBuffer.wrap(pixelBuffer: source)
        let dstBuffer = try MetalBuffer.wrap(pixelBuffer: destination)
        var params = WarpParams(
            sw: Int32(CVPixelBufferGetWidth(source)),
            sh: Int32(CVPixelBufferGetHeight(source)),
            pw: Int32(panoWidth),
            ph: Int32(panoHeight),
            cx: Int32(cropBox.x),
            cy: Int32(cropBox.y),
            cw: Int32(cropBox.w),
            ch: Int32(cropBox.h),
            ow: Int32(CVPixelBufferGetWidth(destination)),
            oh: Int32(CVPixelBufferGetHeight(destination))
        )

        let cb = commandQueue.makeCommandBuffer()!
        let enc = cb.makeComputeCommandEncoder()!
        enc.setComputePipelineState(warpPipeline)
        enc.setBuffer(srcBuffer, offset: 0, index: 0)
        enc.setBuffer(lx, offset: 0, index: 1)
        enc.setBuffer(ly, offset: 0, index: 2)
        enc.setBytes(&params, length: MemoryLayout<WarpParams>.stride, index: 3)
        enc.setBuffer(dstBuffer, offset: 0, index: 4)

        let tg = MTLSize(width: 16, height: 16, depth: 1)
        let groups = MTLSize(
            width: (Int(params.ow) + 15) / 16,
            height: (Int(params.oh) + 15) / 16,
            depth: 1
        )
        enc.dispatchThreadgroups(groups, threadsPerThreadgroup: tg)
        enc.endEncoding()
        cb.commit()
        cb.waitUntilCompleted()   // single-shot per frame; pipelining handled by SegmentProcessor
    }
}
```

### Threadgroup size

`16×16` is the typical sweet spot for Apple GPUs (A-series and M-series).
Validate in E0.B5 — try 8×8, 16×16, 32×8. The right choice depends on
output resolution; `1920×1080` divides evenly enough that `16×16` is fine.

### Pixel buffer pool

`SegmentProcessor` owns a `CVPixelBufferPool` sized to ~3 destination
buffers so consecutive frames can pipeline. The pool lives the lifetime of
one game (output resolution is fixed by render config). Per [[reference_render_backend_findings]]
decode is the iPhone-side bottleneck, so 2–3 destination buffers in flight
is enough.

## Parity test (PipelineTests/WarpKernelTests.swift)

```swift
final class WarpKernelTests: XCTestCase {
    /// E0.B3: per-pixel diff vs cv2.remap baseline within bound.
    func testWarpVsCv2RemapBaseline() throws {
        let baseline = try TestFixtures.loadE0_B3_baseline()  // {source: PNG, mapXY: .npy, cropBox, expected: PNG}
        let renderer = try MetalWarpRenderer(leveledPano: baseline.leveledPano)
        let sourceBuffer = try CVPixelBuffer.from(png: baseline.source, format: .bgra8)
        let destBuffer = try CVPixelBuffer.allocate(
            width: baseline.expected.width, height: baseline.expected.height, format: .bgra8
        )
        try renderer.warp(
            source: sourceBuffer, destination: destBuffer, cropBox: baseline.cropBox
        )
        let outPng = try destBuffer.toPNG()
        let diffStats = imageDiffStats(outPng, baseline.expectedPNG)
        XCTAssertLessThan(diffStats.meanAbsLSB, 1.0)
        XCTAssertLessThan(diffStats.maxAbsLSB, 5.0)
    }
}
```

## Cross-references

- `swift_projection_math.md` — produces `LeveledPano`
- `swift_camera_state_machine.md` — produces `cropBox` per frame
- `segment_pipeline.md` — owns the `MetalWarpRenderer` lifecycle
