/* pgmspace.h stub for plain Linux/aarch64 build of libhelix-aac.
 * On Arduino this provides PROGMEM + pgm_read_*; our Arduino.h stub already
 * defines all of them as flat-memory no-ops, so just pull that in. */
#ifndef PGMSPACE_COMPAT_STUB_H
#define PGMSPACE_COMPAT_STUB_H
#include "Arduino.h"
#endif
