/*
 * On-device deadline and blocking-time monitors (phase 2, step 4).
 *
 * Both detect at the moment a bound is exceeded (k_timer expiry), not when
 * the late work eventually completes.
 */

#include <zephyr/kernel.h>

#include "monitor.h"
#include "trace.h"

static void deadline_expired(struct k_timer *timer)
{
	struct mon_deadline *m = CONTAINER_OF(timer, struct mon_deadline, timer);

	m->misses++;
	trace_mon(m->thread, "deadline miss");
	/* In ISR context k_current_get() is the thread that was interrupted. */
	trace_mon(k_current_get(), "running at alarm");
}

void mon_deadline_init(struct mon_deadline *m, struct k_thread *thread)
{
	k_timer_init(&m->timer, deadline_expired, NULL);
	m->thread = thread;
	m->misses = 0;
}

void mon_deadline_arm(struct mon_deadline *m, int64_t deadline_ms)
{
	k_timer_start(&m->timer, K_TIMEOUT_ABS_MS(deadline_ms), K_NO_WAIT);
}

void mon_deadline_done(struct mon_deadline *m)
{
	k_timer_stop(&m->timer);
}

/*
 * The snapshot says what kind of blocking this is: if the owner is running
 * (at its own or an inherited priority), the wait is bounded by its critical
 * section; if some third thread is running instead, it's unbounded priority
 * inversion. The analysis tools classify it from these records.
 */
static void block_expired(struct k_timer *timer)
{
	struct mon_block *b = CONTAINER_OF(timer, struct mon_block, timer);
	struct k_thread *owner = b->mutex->owner;

	b->alarms++;
	trace_mon(b->waiter, "blocked too long");
	trace_mon(k_current_get(), "running at alarm");
	if (owner != NULL) {
		trace_mon(owner, "mutex owner");
	}
}

void mon_block_init(struct mon_block *b, struct k_thread *waiter)
{
	k_timer_init(&b->timer, block_expired, NULL);
	b->waiter = waiter;
	b->mutex = NULL;
	b->alarms = 0;
}

void mon_block_begin(struct mon_block *b, struct k_mutex *mutex, uint32_t budget_ms)
{
	b->mutex = mutex;
	k_timer_start(&b->timer, K_MSEC(budget_ms), K_NO_WAIT);
}

void mon_block_end(struct mon_block *b)
{
	k_timer_stop(&b->timer);
}
