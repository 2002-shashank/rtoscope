/*
 * Step 2: three periodic threads at different preemptive priorities.
 *
 * Each thread loops: sleep until its release, log "start", burn some CPU time
 * ("work"), log "done". Periods collide, so a higher-priority thread wakes up
 * while a lower one is mid-work and preempts it.
 *
 * The run lasts CONFIG_RTOSCOPE_PERIODIC_HYPERPERIODS hyperperiods, then every
 * thread exits and main dumps the scheduler trace (trace.c). Nothing is
 * printed during the run.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "burn.h"
#include "trace.h"

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

/* No job is released at or after this time (uptime ms). */
static int64_t end_ms;

static void task_entry(void *p1, void *p2, void *p3)
{
	const struct task_cfg *cfg = p1;
	int64_t next = release_ms;

	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	while (next < end_ms) {
		/* Absolute release time: a relative k_msleep(period) would drift by
		 * however long the work (and any preemption) took.
		 */
		k_sleep(K_TIMEOUT_ABS_MS(next));

		trace_app("start");
		burn_cpu_ms(cfg->work_ms);
		trace_app("done");

		next += cfg->period_ms;
	}
}

static uint32_t gcd(uint32_t a, uint32_t b)
{
	while (b != 0) {
		uint32_t t = a % b;

		a = b;
		b = t;
	}
	return a;
}

int main(void)
{
	uint32_t hyper_ms = 1;

	for (int i = 0; i < NUM_TASKS; i++) {
		hyper_ms = hyper_ms / gcd(hyper_ms, tasks[i].period_ms) * tasks[i].period_ms;
	}

	/* The analysis script reads the task set from here, not from a copy. */
	printk("rtoscope periodic: %d hyperperiods of %u ms\n",
	       CONFIG_RTOSCOPE_PERIODIC_HYPERPERIODS, hyper_ms);
	printk("# task,name,prio,work_ms,period_ms\n");
	for (int i = 0; i < NUM_TASKS; i++) {
		printk("# task,%s,%d,%u,%u\n", tasks[i].name, tasks[i].prio,
		       tasks[i].work_ms, tasks[i].period_ms);
	}

	/* All three sleep until the same release time, slightly in the future,
	 * so the first period starts together no matter how long thread
	 * creation takes. They then wake together and the scheduler picks H.
	 */
	release_ms = k_uptime_get() + 10;
	end_ms = release_ms + (int64_t)CONFIG_RTOSCOPE_PERIODIC_HYPERPERIODS * hyper_ms;
	trace_start();

	for (int i = 0; i < NUM_TASKS; i++) {
		k_tid_t tid = k_thread_create(&threads[i], stacks[i],
					      K_THREAD_STACK_SIZEOF(stacks[i]),
					      task_entry, (void *)&tasks[i], NULL, NULL,
					      tasks[i].prio, 0, K_NO_WAIT);
		k_thread_name_set(tid, tasks[i].name);
	}

	for (int i = 0; i < NUM_TASKS; i++) {
		k_thread_join(&threads[i], K_FOREVER);
	}

	trace_stop();
	trace_dump();
	return 0;
}
