#ifndef RTOSCOPE_WATCHDOG_H
#define RTOSCOPE_WATCHDOG_H

#include <stdbool.h>
#include <stdint.h>

/*
 * Task watchdog (one channel per thread) with TIMG0's hardware watchdog as a
 * fallback, plus "last words" in RTC memory that survive the reset.
 */

/* Print the reset cause and whatever the previous boot left in RTC memory.
 * Returns true if this boot follows a watchdog test reset, in which case the
 * fault must not be injected again (no reset loop).
 */
bool watchdog_boot_report(void);

/* task_wdt_init() with the hardware fallback. */
int watchdog_start(void);

/* A channel for the calling thread; on expiry the last words name it. */
int watchdog_add(const char *name, uint32_t timeout_ms);
void watchdog_feed(int channel);
void watchdog_remove(int channel);

/* Record the fault about to be injected, so the next boot can report it. */
void watchdog_note_fault(const char *what);

/* Optional extra output from the expiry handler, before the reboot. */
void watchdog_set_report_hook(void (*hook)(void));

#endif
