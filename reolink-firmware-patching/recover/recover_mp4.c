/* recover_mp4.c — rebuild a power-cut HEVC recording's moov from its mdat.
 *
 * A power-cut leaves ftyp + (skip placeholder) + mdat, with no moov, so the camera
 * discards it. This walks the mdat, reconstructs the VIDEO sample table (conservative
 * NAL-chaining so interleaved AAC audio is never mislabeled as video), copies the codec
 * config (hvc1/hvcC) + box templates from a REFERENCE good recording on the card, and
 * APPENDS a valid moov. The file then plays and the camera re-indexes it as normal.
 *
 * AUDIO (best-effort): the camera interleaves the stream as V-A-V-A... where each audio
 * chunk is a run of raw AAC-LC frames (no ADTS, no length prefix) that begins exactly
 * where the preceding video chunk ends. The same NAL walk that skips audio to resync the
 * video therefore hands us the exact byte range of every audio chunk. We split each chunk
 * into frames with the Helix AAC decoder (bytes-consumed per AACDecode == one frame size)
 * and emit a second 'soun' trak. If audio can't be parsed (corrupt / no ref audio trak),
 * we silently fall back to video-only — audio never blocks video recovery.
 *
 * Video frames are spaced at a fixed FPS (the camera's configured 20 fps) — exact
 * per-frame timing lives only in the lost moov, so constant-rate is the correct recovery.
 *
 * Usage: recover_mp4 <orphan.mp4> <reference_good.mp4>   (edits orphan in place)
 * Builds for the camera with: aarch64-linux-gnu-gcc -O2 -static (link libhelixaac.a).
 *
 * Exit: 0 = recovered, 2 = nothing to do (already has moov / no video), 1 = error.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>   /* ftruncate */
#ifndef NO_AUDIO
#include "aacdec.h"
#endif

#define FPS 20
#define MAXSAMP 400000
#define MAXGAP  20000
#define AAC_SAMPLES_PER_FRAME 1024
#define MIN_AAC_FRAME 16     /* a content-bearing 1024-sample AAC-LC frame is never this small */
#define MAX_AAC_FRAME 768    /* spec cap: 6144 bits per SCE (768 bytes/channel) */

static uint8_t *D; static long DLEN;
static uint32_t rd32(long p){return (D[p]<<24)|(D[p+1]<<16)|(D[p+2]<<8)|D[p+3];}
static uint64_t rd64(long p){return ((uint64_t)rd32(p)<<32)|rd32(p+4);}
static uint32_t rd32_r(uint8_t*b,long p){return (b[p]<<24)|(b[p+1]<<16)|(b[p+2]<<8)|b[p+3];}
static uint16_t rd16_r(uint8_t*b,long p){return (b[p]<<8)|b[p+1];}

/* HEVC NAL helpers (functions, not macros, to avoid name-collision bugs) */
static long g_md1;
static int valid_nal(long p, long *len){          /* returns NAL type, or -1 */
    if(p+5>g_md1) return -1;
    long l=rd32(p);
    if(l<8||l>4000000||p+4+l>g_md1) return -1;
    uint8_t h=D[p+4];
    if(h&0x80) return -1;                          /* forbidden_zero_bit */
    int t=(h>>1)&0x3f;
    if(t>40) return -1;
    *len=l; return t;
}
static int chain(long q,int need){                /* are there >=need chained valid NALs? */
    long l;
    for(int i=0;i<need;i++){ int t=valid_nal(q,&l); if(t<0) return 0; q+=4+l; }
    return 1;
}

/* find a top-level (or within [s,e)) child box by tag; returns offset or -1, sets *bsz,*bhdr */
static long box_in(uint8_t *b, long s, long e, const char *tag, long *bsz, long *bhdr){
    long o=s;
    while(o+8<=e){
        long sz=(b[o]<<24)|(b[o+1]<<16)|(b[o+2]<<8)|b[o+3];
        long hdr=8;
        if(sz==1){ sz=(long)(((uint64_t)((b[o+8]<<24)|(b[o+9]<<16)|(b[o+10]<<8)|b[o+11])<<32)|((uint32_t)((b[o+12]<<24)|(b[o+13]<<16)|(b[o+14]<<8)|b[o+15]))); hdr=16; }
        else if(sz==0) sz=e-o;
        if(!memcmp(b+o+4,tag,4)){ *bsz=sz; *bhdr=hdr; return o; }
        if(sz<=0) break;
        o+=sz;
    }
    return -1;
}
/* find the Nth (0-based) child trak in [s,e); returns offset or -1 */
static long box_nth(uint8_t *b, long s, long e, const char *tag, int n, long *bsz, long *bhdr){
    long o=s; int seen=0;
    while(o+8<=e){
        long sz=(b[o]<<24)|(b[o+1]<<16)|(b[o+2]<<8)|b[o+3]; long hdr=8;
        if(sz==1){ hdr=16; sz=(long)(((uint64_t)rd32_r(b,o+8)<<32)|rd32_r(b,o+12)); }
        else if(sz==0) sz=e-o;
        if(!memcmp(b+o+4,tag,4)){ if(seen==n){ *bsz=sz; *bhdr=hdr; return o; } seen++; }
        if(sz<=0) break;
        o+=sz;
    }
    return -1;
}
/* recursive descent through container boxes to find a nested tag */
static long box_find(uint8_t *b, long s, long e, const char *tag, long *bsz, long *bhdr){
    long o=s;
    while(o+8<=e){
        long sz=(b[o]<<24)|(b[o+1]<<16)|(b[o+2]<<8)|b[o+3]; long hdr=8;
        if(sz==1){ hdr=16; sz=(long)(((uint64_t)rd32_r(b,o+8)<<32)|rd32_r(b,o+12)); }
        else if(sz==0) sz=e-o;
        if(sz<8) break;
        const char *t=(const char*)(b+o+4);
        if(!memcmp(t,tag,4)){ *bsz=sz; *bhdr=hdr; return o; }
        if(!memcmp(t,"moov",4)||!memcmp(t,"trak",4)||!memcmp(t,"mdia",4)||!memcmp(t,"minf",4)||!memcmp(t,"stbl",4)){
            long r=box_find(b,o+hdr,o+sz,tag,bsz,bhdr); if(r>=0) return r;
        }
        o+=sz;
    }
    return -1;
}
/* hdlr handler-type ('vide'/'soun') of a trak at [to,to+tsz) */
static int trak_hdlr(uint8_t*b,long to,long tsz,char out[5]){
    long sz,hd; long h=box_find(b,to+8,to+tsz,"hdlr",&sz,&hd);
    if(h<0) return -1;
    memcpy(out,b+h+16,4); out[4]=0; return 0;
}

/* growable output buffer */
static uint8_t *OB; static long OL, OC;
static void ob_need(long n){ if(OL+n>OC){ OC=(OL+n)*2+1024; OB=realloc(OB,OC);} }
static void ob_bytes(const uint8_t*p,long n){ ob_need(n); memcpy(OB+OL,p,n); OL+=n; }
static void ob_u32(uint32_t v){ uint8_t b[4]={v>>24,v>>16,v>>8,v}; ob_bytes(b,4); }
/* write a box header placeholder, return position to backpatch size */
static long ob_box_begin(const char*tag){ long at=OL; ob_u32(0); ob_bytes((const uint8_t*)tag,4); return at; }
static void ob_box_end(long at){ uint32_t sz=OL-at; OB[at]=sz>>24;OB[at+1]=sz>>16;OB[at+2]=sz>>8;OB[at+3]=sz; }

/* video sample tables (file-scope so they don't blow the stack) */
static long s_off[MAXSAMP]; static uint32_t s_sz[MAXSAMP]; static uint8_t s_key[MAXSAMP];
static long c_off[MAXSAMP]; static uint32_t c_n[MAXSAMP];
/* audio gaps (raw AAC byte ranges between video chunks) */
static long g_off[MAXGAP]; static long g_len[MAXGAP];
/* audio sample + chunk tables */
static long a_off[MAXSAMP]; static uint32_t a_sz[MAXSAMP];
static long ac_off[MAXGAP]; static uint32_t ac_n[MAXGAP];

int main(int argc,char**argv){
    if(argc<3){ fprintf(stderr,"usage: %s <orphan.mp4> <reference.mp4>\n",argv[0]); return 1; }
    int g_dbg = getenv("REC_DEBUG")!=NULL;
    /* load orphan */
    FILE*f=fopen(argv[1],"rb"); if(!f){perror("orphan");return 1;}
    fseek(f,0,SEEK_END); DLEN=ftell(f); fseek(f,0,SEEK_SET);
    D=malloc(DLEN); if(fread(D,1,DLEN,f)!=(size_t)DLEN){fclose(f);return 1;} fclose(f);

    long bsz,bhdr;
    /* already valid? if a moov exists, nothing to do */
    if(box_in(D,0,DLEN,"moov",&bsz,&bhdr)>=0){ fprintf(stderr,"already has moov; skip\n"); return 2; }
    long mdat=box_in(D,0,DLEN,"mdat",&bsz,&bhdr);
    if(mdat<0){ fprintf(stderr,"no mdat\n"); return 3; }   /* unrecoverable */
    long md0=mdat+bhdr, md1=mdat+bsz; if(md1>DLEN) md1=DLEN;
    g_md1=md1;

    /* ---- conservative video walk (records audio gaps as a side effect) ---- */
    long N=0, NC=0, NG=0; long cur_coff=-1; uint32_t cur_cn=0;
    long p=md0;
    while(p+8<=md1){
        long ln; int t=valid_nal(p,&ln);
        if(t>=0 && chain(p,1)){
            long au_off=p; uint32_t au_sz=ln+4; uint8_t is_key=(t>=16&&t<=23)?1:0; p+=4+ln;
            while(p+8<=md1){
                long ln2; int t2=valid_nal(p,&ln2);
                if(t2<0) break;
                int fs2=(t2<=31)?((D[p+6]>>7)&1):0;
                int starter=(t2==32||t2==33||t2==34||t2==35||t2==39)||(t2<=31&&fs2==1);
                if(starter) break;
                au_sz+=ln2+4; if(t2>=16&&t2<=23) is_key=1; p+=4+ln2;
            }
            if(cur_coff<0){ cur_coff=au_off; cur_cn=0; }
            if(N<MAXSAMP){ s_off[N]=au_off; s_sz[N]=au_sz; s_key[N]=is_key; N++; cur_cn++; }
            if(!chain(p,2)){ if(NC<MAXSAMP){ c_off[NC]=cur_coff; c_n[NC]=cur_cn; NC++; } cur_coff=-1; }
        } else {
            if(cur_coff>=0){ if(NC<MAXSAMP){ c_off[NC]=cur_coff; c_n[NC]=cur_cn; NC++; } cur_coff=-1; }
            long q=p+1; int found=0;
            while(q+8<=md1){ if(chain(q,5)){found=1;break;} q++; }
            long gend = found ? q : md1;            /* gap = audio chunk (or trailing audio) */
            if(gend>p && NG<MAXGAP){ g_off[NG]=p; g_len[NG]=gend-p; NG++;
                if(g_dbg) fprintf(stderr,"GAP %ld off=%ld len=%ld found=%d\n",NG-1,p,gend-p,found); }
            if(!found) break;
            p=q;
        }
    }
    if(cur_coff>=0){ if(NC<MAXSAMP){ c_off[NC]=cur_coff; c_n[NC]=cur_cn; NC++; } }
    if(N<1){ fprintf(stderr,"no video samples recovered\n"); return 3; }  /* unrecoverable */
    long NK=0; for(long i=0;i<N;i++) if(s_key[i]) NK++;
    fprintf(stderr,"recovered %ld video samples in %ld chunks, %ld keyframes, %ld audio gaps\n",N,NC,NK,NG);

    /* ---- reference templates ---- */
    FILE*rf=fopen(argv[2],"rb"); if(!rf){perror("reference");return 1;}
    fseek(rf,0,SEEK_END); long RLEN=ftell(rf); fseek(rf,0,SEEK_SET);
    uint8_t*R=malloc(RLEN); if(fread(R,1,RLEN,rf)!=(size_t)RLEN){fclose(rf);return 1;} fclose(rf);
    long rmoov=box_in(R,0,RLEN,"moov",&bsz,&bhdr); if(rmoov<0){fprintf(stderr,"ref no moov\n");return 1;}
    long rms=rmoov+bhdr, rme=rmoov+bsz;
    long sz,hd;
    long o_mvhd=box_find(R,rms,rme,"mvhd",&sz,&hd); long mvhd_sz=sz;
    /* locate the VIDEO trak (by hdlr, not position) */
    long o_vtrak=-1, vt_sz=0; int ti=0; long tsz_i,thd_i;
    for(;;){ long to=box_nth(R,rms,rme,"trak",ti,&tsz_i,&thd_i); if(to<0) break;
             char ht[5]; if(trak_hdlr(R,to,tsz_i,ht)==0 && !memcmp(ht,"vide",4)){o_vtrak=to;vt_sz=tsz_i;break;} ti++; }
    if(o_vtrak<0){ fprintf(stderr,"ref no video trak\n"); return 1; }
    long ts=o_vtrak+8, te=o_vtrak+vt_sz;
    long o_tkhd=box_find(R,ts,te,"tkhd",&sz,&hd); long tkhd_sz=sz;
    long o_mdhd=box_find(R,ts,te,"mdhd",&sz,&hd); long mdhd_sz=sz;
    long o_hdlr=box_find(R,ts,te,"hdlr",&sz,&hd); long hdlr_sz=sz;
    long o_vmhd=box_find(R,ts,te,"vmhd",&sz,&hd); long vmhd_sz=sz;
    long o_dinf=box_find(R,ts,te,"dinf",&sz,&hd); long dinf_sz=sz;
    long o_stsd=box_find(R,ts,te,"stsd",&sz,&hd); long stsd_sz=sz;
    if(o_mvhd<0||o_tkhd<0||o_mdhd<0||o_hdlr<0||o_vmhd<0||o_dinf<0||o_stsd<0){ fprintf(stderr,"ref missing boxes\n"); return 1; }
    /* timescales */
    long mdp=o_mdhd+8; int mver=R[mdp];
    uint32_t media_ts = mver==0 ? rd32_r(R,mdp+12) : rd32_r(R,mdp+20);
    long mvp=o_mvhd+8; int vver=R[mvp];
    uint32_t movie_ts = vver==0 ? rd32_r(R,mvp+12) : rd32_r(R,mvp+20);
    if(media_ts==0) media_ts=90000; if(movie_ts==0) movie_ts=1000;
    uint32_t delta = media_ts/FPS; if(delta==0) delta=media_ts/20+1;
    uint32_t dur_media=(uint32_t)((uint64_t)N*delta);
    uint32_t dur_movie=(uint32_t)((uint64_t)dur_media*movie_ts/media_ts);
    fprintf(stderr,"media_ts=%u movie_ts=%u delta=%u dur=%us\n",media_ts,movie_ts,delta,dur_media/ (media_ts?media_ts:1));

    /* ---- AUDIO (best-effort): find ref audio trak + split gaps with Helix ---- */
    long NA=0, NAC=0; uint32_t aud_ts=16000, aud_movie_dur=0, aud_media_dur=0;
    long o_atrak=-1, at_sz=0;
    long o_atkhd=0,a_tkhd_sz=0,o_amdhd=0,a_mdhd_sz=0,o_ahdlr=0,a_hdlr_sz=0;
    long o_asmhd=0,a_smhd_sz=0,o_adinf=0,a_dinf_sz=0,o_astsd=0,a_stsd_sz=0;
#ifndef NO_AUDIO
    if(NG>0){
        ti=0;
        for(;;){ long to=box_nth(R,rms,rme,"trak",ti,&tsz_i,&thd_i); if(to<0) break;
                 char ht[5]; if(trak_hdlr(R,to,tsz_i,ht)==0 && !memcmp(ht,"soun",4)){o_atrak=to;at_sz=tsz_i;break;} ti++; }
    }
    if(o_atrak>0){
        long as=o_atrak+8, ae=o_atrak+at_sz;
        o_atkhd=box_find(R,as,ae,"tkhd",&a_tkhd_sz,&hd);
        o_amdhd=box_find(R,as,ae,"mdhd",&a_mdhd_sz,&hd);
        o_ahdlr=box_find(R,as,ae,"hdlr",&a_hdlr_sz,&hd);
        o_asmhd=box_find(R,as,ae,"smhd",&a_smhd_sz,&hd);
        o_adinf=box_find(R,as,ae,"dinf",&a_dinf_sz,&hd);
        o_astsd=box_find(R,as,ae,"stsd",&a_stsd_sz,&hd);
        if(o_atkhd<0||o_amdhd<0||o_ahdlr<0||o_asmhd<0||o_adinf<0||o_astsd<0){
            fprintf(stderr,"ref audio trak incomplete; recovering video only\n"); o_atrak=-1;
        }
    }
    if(o_atrak>0){
        long amp=o_amdhd+8; int amver=R[amp];
        aud_ts = amver==0 ? rd32_r(R,amp+12) : rd32_r(R,amp+20);
        if(aud_ts==0) aud_ts=16000;
        int ach = rd16_r(R, o_astsd+16+24);          /* mp4a sample-entry channelcount */
        if(ach<1||ach>2) ach=1;
        HAACDecoder h=AACInitDecoder();
        if(!h){ fprintf(stderr,"AACInitDecoder failed; video only\n"); o_atrak=-1; }
        else {
            AACFrameInfo fi; for(unsigned i=0;i<sizeof(fi);i++) ((char*)&fi)[i]=0;
            fi.nChans=ach; fi.sampRateCore=(int)aud_ts; fi.profile=AAC_PROFILE_LC;
            if(AACSetRawBlockParams(h,0,&fi)){ fprintf(stderr,"AACSetRawBlockParams failed; video only\n"); o_atrak=-1; }
            else {
                static short outbuf[AAC_MAX_NSAMPS*AAC_MAX_NCHANS];
                for(long gi=0; gi<NG && NA<MAXSAMP; gi++){
                    unsigned char *pp = D + g_off[gi];
                    int left = (int)g_len[gi];
                    uint32_t frames_here=0; long chunk_first=-1;
                    while(left>0 && NA<MAXSAMP){
                        long foff = pp - D;
                        int before=left;
                        int err=AACDecode(h,&pp,&left,outbuf);
                        int consumed=before-left;
                        if(err!=ERR_AAC_NONE || consumed<=0) break;  /* underflow/corrupt -> end chunk */
                        /* Each audio chunk is trailed by an unreferenced padding hole. Helix
                         * decodes its leading byte as a degenerate 1-byte element that still
                         * reports outputSamps=1024, so size is the only reliable discriminator:
                         * a real 1024-sample AAC-LC frame carries ICS/scalefactor/spectral
                         * syntax (here 218-315 B; spec cap 768 B / SCE). Anything tiny is the
                         * hole — stop this chunk (raw AAC can't resync past a bad frame). */
                        AACFrameInfo lfi; AACGetLastFrameInfo(h,&lfi);
                        if(g_dbg&&gi==0) fprintf(stderr,"  f n=%u consumed=%d outSamps=%d ch=%d sr=%d\n",frames_here,consumed,lfi.outputSamps,lfi.nChans,lfi.sampRateOut);
                        if(consumed<MIN_AAC_FRAME || consumed>MAX_AAC_FRAME) break;
                        if(lfi.outputSamps!=AAC_SAMPLES_PER_FRAME) break;
                        if(lfi.nChans!=ach || (uint32_t)lfi.sampRateOut!=aud_ts) break;
                        if(chunk_first<0) chunk_first=foff;
                        a_off[NA]=foff; a_sz[NA]=(uint32_t)consumed; NA++; frames_here++;
                    }
                    if(frames_here>0 && NAC<MAXGAP){ ac_off[NAC]=chunk_first; ac_n[NAC]=frames_here; NAC++; }
                    if(g_dbg) fprintf(stderr,"AGAP gi=%ld goff=%ld glen=%ld frames=%u first=%ld\n",gi,g_off[gi],g_len[gi],frames_here,chunk_first);
                }
                AACFreeDecoder(h);
            }
        }
        if(NA<1){ fprintf(stderr,"no audio frames decoded; video only\n"); o_atrak=-1; }
        else {
            aud_media_dur=(uint32_t)((uint64_t)NA*AAC_SAMPLES_PER_FRAME);
            aud_movie_dur=(uint32_t)((uint64_t)aud_media_dur*movie_ts/aud_ts);
            fprintf(stderr,"recovered %ld audio frames in %ld chunks (%u Hz, %dch)\n",NA,NAC,aud_ts,ach);
            /* Sync video to the reliable audio clock: same wall-clock span, evenly spaced.
             * The lost moov held exact per-frame deltas; the recorded rate varies with
             * exposure (often well below the configured max FPS), so the audio duration is
             * a far better basis than a fixed-FPS guess and keeps A/V in sync. */
            uint64_t span_media = (uint64_t)aud_media_dur * media_ts / aud_ts;  /* audio span in video ts */
            uint32_t nd = (uint32_t)(span_media / (uint64_t)(N>0?N:1));
            if(nd>0){
                delta=nd;
                dur_media=(uint32_t)((uint64_t)N*delta);
                dur_movie=(uint32_t)((uint64_t)dur_media*movie_ts/media_ts);
                fprintf(stderr,"video synced to audio clock: delta=%u dur=%us (%.1f fps)\n",
                        delta,dur_media/(media_ts?media_ts:1),(double)media_ts/delta);
            }
        }
    }
#endif

    /* ---- build moov ---- */
    long mv=ob_box_begin("moov");
      /* mvhd (copy + patch movie duration = max of video/audio) */
      { uint32_t md_dur = dur_movie>aud_movie_dur?dur_movie:aud_movie_dur;
        long at=OL; ob_bytes(R+o_mvhd,mvhd_sz); long pp=at+8; int v=OB[pp];
        long doff = v==0 ? pp+16 : pp+24; OB[doff]=md_dur>>24;OB[doff+1]=md_dur>>16;OB[doff+2]=md_dur>>8;OB[doff+3]=md_dur; }
      /* ---- VIDEO trak ---- */
      long tk=ob_box_begin("trak");
        { long at=OL; ob_bytes(R+o_tkhd,tkhd_sz); long pp=at+8; int v=OB[pp];
          long doff = v==0 ? pp+20 : pp+28; OB[doff]=dur_movie>>24;OB[doff+1]=dur_movie>>16;OB[doff+2]=dur_movie>>8;OB[doff+3]=dur_movie; }
        long md=ob_box_begin("mdia");
          { long at=OL; ob_bytes(R+o_mdhd,mdhd_sz); long pp=at+8; int v=OB[pp];
            long doff = v==0 ? pp+16 : pp+24; OB[doff]=dur_media>>24;OB[doff+1]=dur_media>>16;OB[doff+2]=dur_media>>8;OB[doff+3]=dur_media; }
          ob_bytes(R+o_hdlr,hdlr_sz);
          long mi=ob_box_begin("minf");
            ob_bytes(R+o_vmhd,vmhd_sz);
            ob_bytes(R+o_dinf,dinf_sz);
            long st=ob_box_begin("stbl");
              ob_bytes(R+o_stsd,stsd_sz);                      /* stsd (hvc1+hvcC) verbatim */
              { long a=ob_box_begin("stts"); ob_u32(0); ob_u32(1); ob_u32(N); ob_u32(delta); ob_box_end(a); }
              { long a=ob_box_begin("stsc"); ob_u32(0);
                long cntpos=OL; ob_u32(0); uint32_t ne=0; uint32_t lastn=0xffffffff;
                for(long i=0;i<NC;i++){ if(c_n[i]!=lastn){ ob_u32((uint32_t)i+1); ob_u32(c_n[i]); ob_u32(1); lastn=c_n[i]; ne++; } }
                OB[cntpos]=ne>>24;OB[cntpos+1]=ne>>16;OB[cntpos+2]=ne>>8;OB[cntpos+3]=ne; ob_box_end(a); }
              { long a=ob_box_begin("stsz"); ob_u32(0); ob_u32(0); ob_u32(N);
                for(long i=0;i<N;i++) ob_u32(s_sz[i]); ob_box_end(a); }
              { long a=ob_box_begin("stco"); ob_u32(0); ob_u32(NC);
                for(long i=0;i<NC;i++) ob_u32((uint32_t)c_off[i]); ob_box_end(a); }
              { long a=ob_box_begin("stss"); ob_u32(0); ob_u32(NK);
                for(long i=0;i<N;i++) if(s_key[i]) ob_u32((uint32_t)(i+1)); ob_box_end(a); }
            ob_box_end(st);
          ob_box_end(mi);
        ob_box_end(md);
      ob_box_end(tk);
      /* ---- AUDIO trak (only if we decoded frames) ---- */
      if(o_atrak>0 && NA>0){
        long atk=ob_box_begin("trak");
          { long at=OL; ob_bytes(R+o_atkhd,a_tkhd_sz); long pp=at+8; int v=OB[pp];
            long doff = v==0 ? pp+20 : pp+28; OB[doff]=aud_movie_dur>>24;OB[doff+1]=aud_movie_dur>>16;OB[doff+2]=aud_movie_dur>>8;OB[doff+3]=aud_movie_dur; }
          long amd=ob_box_begin("mdia");
            { long at=OL; ob_bytes(R+o_amdhd,a_mdhd_sz); long pp=at+8; int v=OB[pp];
              long doff = v==0 ? pp+16 : pp+24; OB[doff]=aud_media_dur>>24;OB[doff+1]=aud_media_dur>>16;OB[doff+2]=aud_media_dur>>8;OB[doff+3]=aud_media_dur; }
            ob_bytes(R+o_ahdlr,a_hdlr_sz);
            long ami=ob_box_begin("minf");
              ob_bytes(R+o_asmhd,a_smhd_sz);
              ob_bytes(R+o_adinf,a_dinf_sz);
              long ast=ob_box_begin("stbl");
                ob_bytes(R+o_astsd,a_stsd_sz);                 /* stsd (mp4a+esds) verbatim */
                { long a=ob_box_begin("stts"); ob_u32(0); ob_u32(1); ob_u32((uint32_t)NA); ob_u32(AAC_SAMPLES_PER_FRAME); ob_box_end(a); }
                { long a=ob_box_begin("stsc"); ob_u32(0);
                  long cntpos=OL; ob_u32(0); uint32_t ne=0; uint32_t lastn=0xffffffff;
                  for(long i=0;i<NAC;i++){ if(ac_n[i]!=lastn){ ob_u32((uint32_t)i+1); ob_u32(ac_n[i]); ob_u32(1); lastn=ac_n[i]; ne++; } }
                  OB[cntpos]=ne>>24;OB[cntpos+1]=ne>>16;OB[cntpos+2]=ne>>8;OB[cntpos+3]=ne; ob_box_end(a); }
                { long a=ob_box_begin("stsz"); ob_u32(0); ob_u32(0); ob_u32((uint32_t)NA);
                  for(long i=0;i<NA;i++) ob_u32(a_sz[i]); ob_box_end(a); }
                { long a=ob_box_begin("stco"); ob_u32(0); ob_u32((uint32_t)NAC);
                  for(long i=0;i<NAC;i++) ob_u32((uint32_t)ac_off[i]); ob_box_end(a); }
              ob_box_end(ast);
            ob_box_end(ami);
          ob_box_end(amd);
        ob_box_end(atk);
      }
    ob_box_end(mv);

    /* ---- finalize: fix mdat size to the real (power-cut-truncated) content length,
     * then write the moov right after it. A power-cut leaves the mdat header declaring
     * its original (larger) size; without this fix players hunt for moov past the real
     * data and report "moov atom not found". The mdat bytes don't move, so stco stays
     * valid. ---- */
    long mdat_content = md1 - mdat;
    f=fopen(argv[1],"r+b"); if(!f){perror("finalize");return 1;}
    if(bhdr==8){
        uint8_t hb[4]={(uint8_t)(mdat_content>>24),(uint8_t)(mdat_content>>16),(uint8_t)(mdat_content>>8),(uint8_t)mdat_content};
        fseek(f,mdat,SEEK_SET); if(fwrite(hb,1,4,f)!=4){fclose(f);return 1;}
    } else { /* 64-bit largesize at mdat+8 (size field == 1) */
        uint8_t hb[8]; uint64_t v=(uint64_t)mdat_content;
        for(int i=0;i<8;i++) hb[i]=(uint8_t)(v>>(56-8*i));
        fseek(f,mdat+8,SEEK_SET); if(fwrite(hb,1,8,f)!=8){fclose(f);return 1;}
    }
    fseek(f,md1,SEEK_SET);
    if(fwrite(OB,1,OL,f)!=(size_t)OL){fclose(f);return 1;}
    fflush(f);
    if(ftruncate(fileno(f), md1+OL)!=0){ /* best-effort: leftover tail is harmless if it fails */ }
    fclose(f);
    fprintf(stderr,"mdat=%ld bytes; appended %ld-byte moov; recovery OK\n",mdat_content,OL);
    printf("RECOVERED_DURATION_SEC=%u\n",(unsigned)(dur_media/(media_ts?media_ts:1)));  /* for boot script rename */
    return 0;
}
