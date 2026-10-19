#ifndef RTOSCOPE_TRACE_H
#define RTOSCOPE_TRACE_H

/*
 * Scheduler event trace: Zephyr's user tracing hooks plus app events, stamped
 * with CCOUNT (k_cycle_get_32() on this board) into a RAM ring buffer.
 * Nothing is printed until trace_dump().
 */

/* Start/stop recording. Hooks are cheap no-ops while stopped. */
void trace_start(void);
void trace_stop(void);

/* Record an app-level event for the current thread. what must be a string
 * literal (only the pointer is stored).
 */
void trace_app(const char *what);

/* Record a monitor finding about any thread (not necessarily the current
 * one); safe from ISR context, e.g. a k_timer expiry. prio is the thread's
 * priority at that moment. what must be a string literal.
 */
struct k_thread;
void trace_mon(struct k_thread *thread, const char *what);

/* Print the buffer as CSV over the console, followed by a CRC-32 of the
 * dump ("# crc32 xxxxxxxx"). Call after trace_stop().
 */
void trace_dump(void);

#endif
