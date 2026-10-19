#ifndef RTOSCOPE_MONITOR_H
#define RTOSCOPE_MONITOR_H

/*
 * On-device timing monitors. Each is a one-shot k_timer that fires only if
 * something has gone wrong; the expiry handler (ISR context) records the
 * finding in the trace with trace_mon(). No printing.
 */

#include <zephyr/kernel.h>

/* Deadline monitor: did the job finish by its deadline? (timeliness) */
struct mon_deadline {
	struct k_timer timer;
	struct k_thread *thread;
	uint32_t misses;
};

void mon_deadline_init(struct mon_deadline *m, struct k_thread *thread);

/*
 * Arm for the job released next, at its absolute deadline (uptime ms). Call
 * BEFORE sleeping until the release: if a job only armed itself once it
 * started, a job starved past its own deadline would never be detected.
 */
void mon_deadline_arm(struct mon_deadline *m, int64_t deadline_ms);

/* The job finished: cancel the timer. */
void mon_deadline_done(struct mon_deadline *m);

/* Blocking monitor: was a thread kept waiting on a mutex beyond a budget? */
struct mon_block {
	struct k_timer timer;
	struct k_thread *waiter;
	struct k_mutex *mutex;
	uint32_t alarms;
};

void mon_block_init(struct mon_block *b, struct k_thread *waiter);

/* Call right before k_mutex_lock(mutex) / right after it returns. */
void mon_block_begin(struct mon_block *b, struct k_mutex *mutex, uint32_t budget_ms);
void mon_block_end(struct mon_block *b);

#endif
