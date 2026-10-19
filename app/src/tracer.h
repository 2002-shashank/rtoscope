#ifndef RTOSCOPE_TRACER_H
#define RTOSCOPE_TRACER_H

/*
 * Whichever tracer this build has (phase 2, step 5):
 *   CONFIG_TRACING_USER  trace.c (16 B records, ring, CCOUNT timestamps)
 *   CONFIG_TRACING_CTF   Zephyr's CTF tracer, RAM backend, sync mode
 *   neither              no tracing at all (the baseline)
 */

extern const char *const tracer_name;

void tracer_start(void);
void tracer_stop(void);
void tracer_dump(void);   /* trace.c CSV, or the CTF RAM buffer as hex */

/* Time sys_trace_k_thread_switched_in(), the same hook in both tracers. */
void tracer_bench(void);

#endif
