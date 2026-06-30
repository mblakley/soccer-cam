/* Minimal Arduino.h stub so libhelix-aac (Arduino fork) builds for plain Linux/aarch64.
 * On a flat-memory target PROGMEM is a no-op and pgm_read_* are plain dereferences. */
#ifndef ARDUINO_COMPAT_STUB_H
#define ARDUINO_COMPAT_STUB_H
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#ifndef PROGMEM
#define PROGMEM
#endif
#ifndef PGM_P
#define PGM_P const char *
#endif
#define pgm_read_byte(a)   (*(const unsigned char  *)(a))
#define pgm_read_word(a)   (*(const unsigned short *)(a))
#define pgm_read_dword(a)  (*(const unsigned int   *)(a))
#define pgm_read_float(a)  (*(const float          *)(a))
#define memcpy_P  memcpy
#define memcmp_P  memcmp
#define strcpy_P  strcpy
#define strlen_P  strlen
#endif
