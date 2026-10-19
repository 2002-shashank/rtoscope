#ifndef RTOSCOPE_DUMP_H
#define RTOSCOPE_DUMP_H

#include <stdint.h>

/*
 * Print one line of a dump and fold it into a running CRC-32 (IEEE, same as
 * Python's zlib.crc32). Every dump ends with "# crc32 xxxxxxxx" over its
 * lines: the UART -> USB -> host path can lose bytes (seen once in 690 KB on
 * the CYD's CH340), and a lost digit would still parse.
 *
 * Start with crc = 0; fmt must produce one line ending in "\n", < 128 chars.
 */
uint32_t dump_line(uint32_t crc, const char *fmt, ...);

/* Print the closing "# crc32 xxxxxxxx" line. */
void dump_crc(uint32_t crc);

#endif
