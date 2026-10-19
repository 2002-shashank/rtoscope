/*
 * Step 3: priority inversion, with and without priority inheritance.
 *
 * Classic scenario, run once per boot:
 *   t =  0  L (prio 9) locks the shared mutex, starts a 50 ms critical section
 *   t = 10  H (prio 5) wakes and tries to lock it -> blocks
 *   t = 20  M (prio 7) wakes and burns CPU, never touching the mutex
 *
 * Without PI, M preempts L, so H waits for M's whole run (unbounded).
 * With PI, L runs at H's priority while H waits, so M can't preempt it
 * and H waits only for the rest of L's critical section (bounded).
 *
 * The scheduler trace (trace.c) records every switch, wake-up and priority
 * change during the run; it is printed after all threads finish, so printing
 * doesn't perturb the run.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "burn.h"
#include "monitor.h"
#include "trace.h"

#define STACK_SIZE 2048

#define PRIO_H 5
#define PRIO_M 7
#define PRIO_L 9

#define L_CS_MS    50
#define H_CS_MS    10
#define H_DELAY_MS 10
#define M_DELAY_MS 20

/* Monitor bounds for H (CONFIG_RTOSCOPE_MONITOR): its deadline, relative to
 * its release, and how long it may wait for the mutex: L's whole critical
 * section plus a margin. With PI H waits 40 ms; without, 240 ms.
 */
#define H_DEADLINE_MS     100
#define H_BLOCK_BUDGET_MS (L_CS_MS + 10)

#if defined(CONFIG_RTOSCOPE_LOCK_MUTEX_PI)
#define MODE_NAME "k_mutex, priority inheritance ON"
#else
#define MODE_NAME "k_mutex, priority inheritance OFF (CONFIG_PRIORITY_CEILING)"
#endif

K_MUTEX_DEFINE(resource);

K_THREAD_STACK_DEFINE(stack_h, STACK_SIZE);
K_THREAD_STACK_DEFINE(stack_m, STACK_SIZE);
K_THREAD_STACK_DEFINE(stack_l, STACK_SIZE);
static struct k_thread thread_h, thread_m, thread_l;

static int64_t release_ms;

static struct mon_deadline h_deadline;
static struct mon_block h_block;

static void low_entry(void *p1, void *p2, void *p3)
{
	k_sleep(K_TIMEOUT_ABS_MS(release_ms));
	trace_app("lock request");
	k_mutex_lock(&resource, K_FOREVER);
	trace_app("locked");
	burn_cpu_ms(L_CS_MS);
	trace_app("unlock");
	k_mutex_unlock(&resource);
	trace_app("done");
}

static void high_entry(void *p1, void *p2, void *p3)
{
	const bool mon = IS_ENABLED(CONFIG_RTOSCOPE_MONITOR);

	/* Armed before the release, so even a job that never starts is caught. */
	if (mon) {
		mon_deadline_arm(&h_deadline, release_ms + H_DELAY_MS + H_DEADLINE_MS);
	}
	k_sleep(K_TIMEOUT_ABS_MS(release_ms + H_DELAY_MS));
	trace_app("lock request");
	if (mon) {
		mon_block_begin(&h_block, &resource, H_BLOCK_BUDGET_MS);
	}
	k_mutex_lock(&resource, K_FOREVER);
	if (mon) {
		mon_block_end(&h_block);
	}
	trace_app("locked");
	burn_cpu_ms(H_CS_MS);
	trace_app("unlock");
	k_mutex_unlock(&resource);
	trace_app("done");
	if (mon) {
		mon_deadline_done(&h_deadline);
	}
}

static void medium_entry(void *p1, void *p2, void *p3)
{
	k_sleep(K_TIMEOUT_ABS_MS(release_ms + M_DELAY_MS));
	trace_app("start");
	burn_cpu_ms(CONFIG_RTOSCOPE_M_WORK_MS);
	trace_app("done");
}

int main(void)
{
	printk("rtoscope step 3: %s, M work %d ms, PRIORITY_CEILING=%d\n",
	       MODE_NAME, CONFIG_RTOSCOPE_M_WORK_MS, CONFIG_PRIORITY_CEILING);
	if (IS_ENABLED(CONFIG_RTOSCOPE_MONITOR)) {
		printk("# monitor: H deadline %d ms after release, blocking budget %d ms\n",
		       H_DEADLINE_MS, H_BLOCK_BUDGET_MS);
	}
	mon_deadline_init(&h_deadline, &thread_h);
	mon_block_init(&h_block, &thread_h);

	release_ms = k_uptime_get() + 10;
	trace_start();

	k_thread_create(&thread_l, stack_l, K_THREAD_STACK_SIZEOF(stack_l),
			low_entry, NULL, NULL, NULL, PRIO_L, 0, K_NO_WAIT);
	k_thread_name_set(&thread_l, "L");
	k_thread_create(&thread_m, stack_m, K_THREAD_STACK_SIZEOF(stack_m),
			medium_entry, NULL, NULL, NULL, PRIO_M, 0, K_NO_WAIT);
	k_thread_name_set(&thread_m, "M");
	k_thread_create(&thread_h, stack_h, K_THREAD_STACK_SIZEOF(stack_h),
			high_entry, NULL, NULL, NULL, PRIO_H, 0, K_NO_WAIT);
	k_thread_name_set(&thread_h, "H");

	/* main (prio 0) blocks here; the demo threads run, then exit. */
	k_thread_join(&thread_h, K_FOREVER);
	k_thread_join(&thread_m, K_FOREVER);
	k_thread_join(&thread_l, K_FOREVER);

	trace_stop();
	trace_dump();
	if (IS_ENABLED(CONFIG_RTOSCOPE_MONITOR)) {
		printk("# monitor: H deadline misses %u, blocking alarms %u\n",
		       h_deadline.misses, h_block.alarms);
	}
	return 0;
}
