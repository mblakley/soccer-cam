/* Validate the Helix raw-AAC frame splitter against a ground-truth audio elementary
 * stream extracted from a reference recording's moov.
 *
 * Reads a raw AAC-LC elementary stream (no ADTS, no length prefixes) and uses Helix
 * AACDecode to walk it frame-by-frame, printing the bytes consumed per frame. Those
 * sizes must EXACTLY match the reference stsz before we trust this for audio recovery.
 *
 * Usage: aac_split_test <audio_raw.bin> <nchans> <samplerate>
 */
#include <stdio.h>
#include <stdlib.h>
#include "ESP8266Audio/src/libhelix-aac/aacdec.h"

int main(int argc, char **argv) {
    if (argc < 4) { fprintf(stderr, "usage: %s raw.bin nchans samplerate\n", argv[0]); return 2; }
    int nchans = atoi(argv[2]);
    int srate  = atoi(argv[3]);

    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("open"); return 2; }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    unsigned char *buf = malloc(n);
    if (fread(buf, 1, n, f) != (size_t)n) { perror("read"); return 2; }
    fclose(f);

    HAACDecoder h = AACInitDecoder();
    if (!h) { fprintf(stderr, "AACInitDecoder failed\n"); return 2; }

    AACFrameInfo fi;
    /* zero then set the raw-block params the way an MP4 demuxer would */
    for (unsigned i = 0; i < sizeof(fi); i++) ((char*)&fi)[i] = 0;
    fi.nChans       = nchans;
    fi.sampRateCore = srate;
    fi.profile      = AAC_PROFILE_LC;   /* mp4a AAC-LC */
    int r = AACSetRawBlockParams(h, 0, &fi);
    if (r) { fprintf(stderr, "AACSetRawBlockParams err=%d\n", r); return 2; }

    short out[AAC_MAX_NSAMPS * AAC_MAX_NCHANS];
    unsigned char *p = buf;
    int left = (int)n;
    int frame = 0;
    while (left > 0) {
        int before = left;
        int err = AACDecode(h, &p, &left, out);
        int consumed = before - left;
        if (err == ERR_AAC_INDATA_UNDERFLOW) {
            /* not enough bytes for a full frame -> trailing partial; stop */
            fprintf(stderr, "UNDERFLOW at frame %d, %d bytes left\n", frame, left);
            break;
        }
        if (err != ERR_AAC_NONE) {
            fprintf(stderr, "DECODE ERR=%d at frame %d (consumed %d, %d left)\n",
                    err, frame, consumed, left);
            /* still print what we consumed so the mismatch is visible */
            if (consumed <= 0) break;
        }
        printf("%d\n", consumed);
        frame++;
        if (frame > 100000) break;
    }
    AACFreeDecoder(h);
    fprintf(stderr, "frames=%d  bytes_consumed=%ld/%ld\n", frame, (long)(p-buf), n);
    return 0;
}
