/*
 * Scheduler event trace (goal 4).
 *
 * Implements Zephyr's CONFIG_TRACING_USER hooks (weak no-ops in
 * subsys/tracing/user/tracing_user.c) to record every context switch,
 * wake-up, pend and priority change, plus app events from trace_app() and
 * monitor findings from trace_mon().
 *
 * Hot path rule: a hook only takes a timestamp and stores a 16-byte record.
 * Thread names, formatting and UART output all happen in trace_dump().
 */

#include <zephyr/kernel.h>
#include <zephyr/kernel_structs.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include "dump.h"
#include "trace.h"

enum ev_type {
	EV_IN,     /* thread got the CPU */
	EV_OUT,    /* thread left the CPU; state says why */
	EV_READY,  /* thread became runnable: release, or unblocked */
	EV_PEND,   /* thread started waiting on a kernel object */
	EV_PRIO,   /* priority changed (k_mutex inheritance boost/restore) */
	EV_APP,    /* trace_app() label */
	EV_MON,    /* trace_mon() label: a monitor finding about a thread */
};

static const char *const ev_names[] = { "in", "out", "ready", "pend", "prio", "app", "mon" };

struct rec {
	uint32_t cyc;              /* k_cycle_get_32(): CCOUNT, 240 MHz */
	struct k_thread *thread;
	const char *what;          /* EV_APP only */
	uint8_t type;
	int8_t prio;               /* thread's priority; for EV_PRIO the new one */
	uint8_t state;             /* EV_OUT: outgoing thread's base.thread_state */
	uint8_t pad;
};

/* Power of two, so the slot index is a mask, not a division.
 * 2048 x 16 B = 32 KB: room for several hyperperiods of the periodic demo.
 */
#define TRACE_LEN 2048
BUILD_ASSERT(IS_POWER_OF_TWO(TRACE_LEN));

static struct rec ring[TRACE_LEN];
static uint32_t head;           /* records ever written; slot = head % TRACE_LEN */
static volatile bool armed;
static uint32_t call_cycles;    /* measured cost of one record() */

/*
 * The kernel hooks already run with interrupts masked (Xtensa switch path,
 * or the scheduler spinlock, which on one core is an IRQ lock). trace_app()
 * runs in preemptible thread context, so the slot claim and the write need
 * the lock too. The timestamp is taken inside it so records stay in
 * time order. When full, the oldest records are overwritten.
 */
static void record(uint8_t type, struct k_thread *thread, int prio, uint8_t state,
		   const char *what)
{
	unsigned int key;

	if (!armed) {
		return;
	}

	key = irq_lock();
	ring[head & (TRACE_LEN - 1)] = (struct rec){
		.cyc = k_cycle_get_32(),
		.thread = thread,
		.what = what,
		.type = type,
		.prio = prio,
		.state = state,
	};
	head++;
	irq_unlock(key);
}

/* --- Zephyr user tracing hooks --------------------------------------- */

/* Called before _current changes, so _current is the outgoing thread. */
void sys_trace_thread_switched_out_user(void)
{
	struct k_thread *t = k_current_get();

	record(EV_OUT, t, t->base.prio, t->base.thread_state, NULL);
}

/* Called from the Xtensa switch/ISR-exit asm after _current changes. */
void sys_trace_thread_switched_in_user(void)
{
	struct k_thread *t = k_current_get();

	record(EV_IN, t, t->base.prio, 0, NULL);
}

void sys_trace_thread_sched_ready_user(struct k_thread *thread)
{
	record(EV_READY, thread, thread->base.prio, 0, NULL);
}

void sys_trace_thread_pend_user(struct k_thread *thread)
{
	record(EV_PEND, thread, thread->base.prio, 0, NULL);
}

void sys_trace_thread_priority_set_user(struct k_thread *thread, int prio)
{
	record(EV_PRIO, thread, prio, 0, NULL);
}

/* --- API -------------------------------------------------------------- */

void trace_app(const char *what)
{
	struct k_thread *t = k_current_get();

	record(EV_APP, t, t->base.prio, 0, what);
}

void trace_mon(struct k_thread *thread, const char *what)
{
	record(EV_MON, thread, thread->base.prio, 0, what);
}

void trace_start(void)
{
	head = 0;
	armed = true;

	/* Back-to-back records: the gap between two timestamps is the cost of
	 * one trace call, i.e. how much tracing perturbs the run. Take the
	 * minimum over several: code runs from SPI flash through the cache, so
	 * the first calls pay for cache misses (~1000 cycles, measured).
	 */
	for (int i = 0; i < 8; i++) {
		trace_app("cal");
	}
	call_cycles = UINT32_MAX;
	for (int i = 1; i < 8; i++) {
		call_cycles = MIN(call_cycles, ring[i].cyc - ring[i - 1].cyc);
	}
	head = 0;
}

void trace_stop(void)
{
	armed = false;
}

/* Why the outgoing thread left the CPU. */
static const char *state_name(uint8_t s)
{
	if (s & _THREAD_DEAD) {
		return "dead";
	}
	if (s & _THREAD_PENDING) {
		return "pending";
	}
	if (s & _THREAD_SLEEPING) {
		return "sleeping";
	}
	if (s & _THREAD_SUSPENDED) {
		return "suspended";
	}
	return "ready";  /* still runnable: it was preempted */
}

void trace_dump(void)
{
	uint32_t n = MIN(head, TRACE_LEN);
	uint32_t first = head - n;
	uint32_t t0 = ring[first & (TRACE_LEN - 1)].cyc;
	uint32_t crc = 0;

	crc = dump_line(crc, "# rtoscope trace, cycle clock %u Hz, %u events, %u dropped, "
			"trace call %u cycles\n",
			sys_clock_hw_cycles_per_sec(), n, head - n, call_cycles);
	crc = dump_line(crc, "cycles,thread,event,prio,arg\n");

	for (uint32_t i = first; i < head; i++) {
		const struct rec *r = &ring[i & (TRACE_LEN - 1)];
		const char *name = k_thread_name_get(r->thread);
		const char *arg = r->type == EV_OUT ? state_name(r->state) :
				  r->what != NULL ? r->what : "";

		/* Unsigned subtraction stays correct across one CCOUNT wrap. */
		if (name != NULL && name[0] != '\0') {
			crc = dump_line(crc, "%u,%s,%s,%d,%s\n", r->cyc - t0, name,
					ev_names[r->type], r->prio, arg);
		} else {
			crc = dump_line(crc, "%u,%p,%s,%d,%s\n", r->cyc - t0, r->thread,
					ev_names[r->type], r->prio, arg);
		}
	}

	dump_crc(crc);
}
