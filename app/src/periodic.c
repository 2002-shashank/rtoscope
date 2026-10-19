/*
 * Step 2: three periodic threads at different preemptive priorities.
 *
 * Each thread loops: sleep until its release, log "start", burn some CPU time
 * ("work"), log "done". Periods collide, so a higher-priority thread wakes up
 * while a lower one is mid-work and preempts it.
 *
 * A run lasts CONFIG_RTOSCOPE_PERIODIC_HYPERPERIODS hyperperiods, then every
 * thread exits and main dumps the scheduler trace (trace.c). Nothing is
 * printed during a run. One boot does CONFIG_RTOSCOPE_PERIODIC_RUNS runs.
 *
 * Optional (phase 2, step 4): every job's deadline is monitored
 * (CONFIG_RTOSCOPE_MONITOR), M's 3rd job can overrun on purpose
 * (CONFIG_RTOSCOPE_INJECT_OVERRUN), and each thread can feed its own task
 * watchdog channel, with a controlled fault in M's 4th job
 * (CONFIG_RTOSCOPE_WDT, CONFIG_RTOSCOPE_WDT_TEST_*).
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "burn.h"
#include "monitor.h"
#include "trace.h"
#include "tracer.h"
#if defined(CONFIG_RTOSCOPE_PROBE)
#include "probe.h"
#endif
#if defined(CONFIG_RTOSCOPE_WDT)
#include "watchdog.h"
#endif

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
#define TASK_M    1

#define OVERRUN_JOB 3      /* CONFIG_RTOSCOPE_INJECT_OVERRUN: M's 3rd job... */
#define OVERRUN_MS  300    /* ...burns this much extra: a deadline miss */
#define FAULT_JOB   4      /* CONFIG_RTOSCOPE_WDT_TEST_*: M's 4th job hangs */

K_THREAD_STACK_ARRAY_DEFINE(stacks, NUM_TASKS, STACK_SIZE);
static struct k_thread threads[NUM_TASKS];

/* Common release time (uptime ms) for every thread's first period, set by
 * main before any thread is created. Without it, each thread would anchor its
 * period at its first run, which is later for lower priorities.
 */
static int64_t release_ms;

/* No job is released at or after this time (uptime ms). */
static int64_t end_ms;

static struct mon_deadline deadlines[NUM_TASKS];

/* False on a boot that follows a watchdog test reset: inject only once. */
static bool inject_wdt_fault;

#if defined(CONFIG_RTOSCOPE_WDT)
static void report_misses(void)
{
	printk("# monitor: deadline misses so far H %u, M %u, L %u\n", deadlines[0].misses,
	       deadlines[1].misses, deadlines[2].misses);
}
#endif

/* Never returns: the watchdog has to end it. */
static void wdt_fault(void)
{
#if defined(CONFIG_RTOSCOPE_WDT)
	if (IS_ENABLED(CONFIG_RTOSCOPE_WDT_TEST_STARVE)) {
		/* Preemptible: H keeps running, L starves, M stops feeding. */
		watchdog_note_fault("starve");
		trace_app("fault: starve");
		while (true) {
			arch_nop();
		}
	}
	/* Interrupts locked: no scheduler, no task_wdt timer. Only TIMG0. */
	watchdog_note_fault("hang");
	(void)irq_lock();
	while (true) {
		arch_nop();
	}
#endif
}

static void task_entry(void *p1, void *p2, void *p3)
{
	const struct task_cfg *cfg = p1;
	const int idx = cfg - tasks;
	int64_t next = release_ms;
	int channel = -1;

	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

#if defined(CONFIG_RTOSCOPE_WDT)
	/* 2x the period: one late job is tolerated, a stalled thread is not. */
	channel = watchdog_add(cfg->name, 2 * cfg->period_ms);
#endif

	for (int job = 1; next < end_ms; job++) {
		/* Arm before sleeping: a job starved past its deadline still trips it. */
		if (IS_ENABLED(CONFIG_RTOSCOPE_MONITOR)) {
			mon_deadline_arm(&deadlines[idx], next + cfg->period_ms);
		}

		/* Absolute release time: a relative k_msleep(period) would drift by
		 * however long the work (and any preemption) took.
		 */
		k_sleep(K_TIMEOUT_ABS_MS(next));
#if defined(CONFIG_RTOSCOPE_PROBE)
		probe_wake(idx, job);
#endif

		trace_app("start");
		burn_cpu_ms(cfg->work_ms);
		if (IS_ENABLED(CONFIG_RTOSCOPE_INJECT_OVERRUN) && idx == TASK_M &&
		    job == OVERRUN_JOB) {
			trace_app("overrun");
			burn_cpu_ms(OVERRUN_MS);
		}
		if (inject_wdt_fault && idx == TASK_M && job == FAULT_JOB) {
			wdt_fault();
		}
		trace_app("done");
#if defined(CONFIG_RTOSCOPE_PROBE)
		probe_done(idx, job);
#endif

		if (IS_ENABLED(CONFIG_RTOSCOPE_MONITOR)) {
			mon_deadline_done(&deadlines[idx]);
		}
#if defined(CONFIG_RTOSCOPE_WDT)
		watchdog_feed(channel);
#endif
		next += cfg->period_ms;
	}

#if defined(CONFIG_RTOSCOPE_WDT)
	/* A channel left behind by an exited thread would expire. */
	watchdog_remove(channel);
#else
	ARG_UNUSED(channel);
#endif
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

	printk("# tracer: %s\n", tracer_name);
	/* The analysis script reads the task set from here, not from a copy. */
	printk("rtoscope periodic: %d hyperperiods of %u ms\n",
	       CONFIG_RTOSCOPE_PERIODIC_HYPERPERIODS, hyper_ms);
	printk("# task,name,prio,work_ms,period_ms\n");
	for (int i = 0; i < NUM_TASKS; i++) {
		printk("# task,%s,%d,%u,%u\n", tasks[i].name, tasks[i].prio,
		       tasks[i].work_ms, tasks[i].period_ms);
	}

#if defined(CONFIG_RTOSCOPE_WDT)
	bool recovered = watchdog_boot_report();

	inject_wdt_fault = !recovered && !IS_ENABLED(CONFIG_RTOSCOPE_WDT_TEST_NONE);
	printk("# wdt: test %s, fault %s\n",
	       IS_ENABLED(CONFIG_RTOSCOPE_WDT_TEST_STARVE) ? "starve" :
	       IS_ENABLED(CONFIG_RTOSCOPE_WDT_TEST_HANG) ? "hang" : "none",
	       inject_wdt_fault ? "armed (M, job 4)" : "not armed");
	watchdog_set_report_hook(report_misses);
	if (watchdog_start() != 0) {
		printk("# wdt: task_wdt_init failed\n");
	}
#endif
	if (IS_ENABLED(CONFIG_RTOSCOPE_MONITOR)) {
		printk("# monitor: deadline = period for every job\n");
	}
	for (int i = 0; i < NUM_TASKS; i++) {
		mon_deadline_init(&deadlines[i], &threads[i]);
	}

	for (int run = 1; run <= CONFIG_RTOSCOPE_PERIODIC_RUNS; run++) {
		/* All three sleep until the same release time, slightly in the
		 * future, so the first period starts together no matter how long
		 * thread creation takes. They then wake together and the
		 * scheduler picks H.
		 */
		release_ms = k_uptime_get() + 10;
		end_ms = release_ms + (int64_t)CONFIG_RTOSCOPE_PERIODIC_HYPERPERIODS * hyper_ms;
#if defined(CONFIG_RTOSCOPE_PROBE)
		probe_reset();
#endif
		tracer_start();

		/* Reusing a k_thread is fine once k_thread_join() has seen it exit. */
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

		tracer_stop();
		printk("# run %d/%d\n", run, CONFIG_RTOSCOPE_PERIODIC_RUNS);
		tracer_dump();
#if defined(CONFIG_RTOSCOPE_PROBE)
		{
			const char *names[NUM_TASKS];

			for (int i = 0; i < NUM_TASKS; i++) {
				names[i] = tasks[i].name;
			}
			probe_dump(names, NUM_TASKS);
		}
#endif
		if (IS_ENABLED(CONFIG_RTOSCOPE_MONITOR)) {
			printk("# monitor: deadline misses so far H %u, M %u, L %u\n",
			       deadlines[0].misses, deadlines[1].misses, deadlines[2].misses);
		}
	}

	if (IS_ENABLED(CONFIG_RTOSCOPE_PROBE)) {
		tracer_bench();
	}
	printk("# rtoscope runs done\n");
	return 0;
}
