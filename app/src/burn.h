#ifndef RTOSCOPE_BURN_H
#define RTOSCOPE_BURN_H

#include <stdint.h>

/* Spin until the calling thread has received ms of CPU time. */
void burn_cpu_ms(uint32_t ms);

#endif
