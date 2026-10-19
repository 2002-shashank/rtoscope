#ifndef RTOSCOPE_PROBE_H
#define RTOSCOPE_PROBE_H

/*
 * Tracer-independent timing probe (phase 2, step 5): two CCOUNT reads per
 * job, stored in a static array, so the same measurement runs unchanged
 * whether trace.c, Zephyr's CTF tracer, or no tracer is built in.
 */

void probe_reset(void);
void probe_wake(int task, int job);   /* right after the job's release sleep */
void probe_done(int task, int job);   /* when the job's work is finished */

/* "# rtoscope probe ..." block with CRC; names[i] labels task i. */
void probe_dump(const char *const names[], int ntasks);

#endif
