/*
 * Step 2: three periodic threads at different preemptive priorities.
 *
 * Each thread loops: print "start", burn some CPU time ("work"), print "done",
 * then sleep until its next period. Periods collide, so a higher-priority
 * thread wakes up while a lower one is mid-work and preempts it.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "burn.h"

#define STACK_SIZE 2048

struct task_cfg {
	const char *name;
	int prio;           /* Zephyr: lower number = higher priority */
	uint32_t work_ms;   /* CPU time to burn per period */
	uint32_t period_ms;
};

static const struct task_cfg tasks[] = {
	{ "H", 5,  15,  100 },
	{ "M", 7,  40,  300 },
	{ "L", 9, 180, 1000 },
};

#define NUM_TASKS ARRAY_SIZE(tasks)

K_THREAD_STACK_ARRAY_DEFINE(stacks, NUM_TASKS, STACK_SIZE);
static struct k_thread threads[NUM_TASKS];

/* Common release time (uptime ms) for every thread's first period, set by
 * main before any thread is created. Without it, each thread would anchor its
 * period at its first run, which is later for lower priorities.
 */
static int64_t release_ms;

static void task_entry(void *p1, void *p2, void *p3)
{
	const struct task_cfg *cfg = p1;
	int64_t next = release_ms;

	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	while (1) {
		/* Absolute release time: a relative k_msleep(period) would drift by
		 * however long the work (and any preemption) took.
		 */
		k_sleep(K_TIMEOUT_ABS_MS(next));

		printk("[%6u ms] %s start\n", k_uptime_get_32(), cfg->name);
		burn_cpu_ms(cfg->work_ms);
		printk("[%6u ms] %s done\n", k_uptime_get_32(), cfg->name);

		next += cfg->period_ms;
	}
}

int main(void)
{
	printk("rtoscope step 2: H=%d M=%d L=%d (main=%d)\n",
	       tasks[0].prio, tasks[1].prio, tasks[2].prio,
	       k_thread_priority_get(k_current_get()));

	/* All three sleep until the same release time, slightly in the future,
	 * so the first period starts together no matter how long thread
	 * creation takes. They then wake together and the scheduler picks H.
	 */
	release_ms = k_uptime_get() + 10;

	for (int i = 0; i < NUM_TASKS; i++) {
		k_tid_t tid = k_thread_create(&threads[i], stacks[i],
					      K_THREAD_STACK_SIZEOF(stacks[i]),
					      task_entry, (void *)&tasks[i], NULL, NULL,
					      tasks[i].prio, 0, K_NO_WAIT);
		k_thread_name_set(tid, tasks[i].name);
	}

	return 0;
}
