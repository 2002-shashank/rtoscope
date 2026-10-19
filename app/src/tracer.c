/*
 * Tracer selection and comparison helpers (phase 2, step 5).
 */

#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include "dump.h"
#include "trace.h"
#include "tracer.h"

#if defined(CONFIG_TRACING_USER)
const char *const tracer_name = "user";
#elif defined(CONFIG_TRACING_CTF)
const char *const tracer_name = IS_ENABLED(CONFIG_TRACING_ISR) ? "ctf" : "ctf-noisr";
#else
const char *const tracer_name = "none";
#endif

#if defined(CONFIG_TRACING_CTF)
/* subsys/tracing/include/tracing_core.h; CTF starts disabled with
 * CONFIG_TRACING_HANDLE_HOST_CMD, so the run can be armed like trace.c.
 */
void tracing_cmd_handle(uint8_t *buf, uint32_t length);

static void ctf_cmd(const char *cmd)
{
	tracing_cmd_handle((uint8_t *)cmd, strlen(cmd));
}

/* The RAM backend's buffer (subsys/tracing/tracing_backend_ram.c). It is
 * linear: once full, further events are silently dropped.
 */
extern uint8_t ram_tracing[CONFIG_RAM_TRACING_BUFFER_SIZE];

static void ctf_dump(void)
{
	const size_t size = CONFIG_RAM_TRACING_BUFFER_SIZE;
	size_t used = size;
	uint32_t crc = 0;
	char hex[2 * 32 + 1];

	while (used > 0 && ram_tracing[used - 1] == 0) {
		used--;
	}
	/* The last event can end in zero bytes (name padding, a 0 field):
	 * keep one maximum-size event of margin. The decoder stops at ID 0.
	 */
	used = MIN(used + 40, size);

	crc = dump_line(crc, "# rtoscope ctf, ram buffer %u bytes, %u dumped, isr events %s\n",
			size, used, IS_ENABLED(CONFIG_TRACING_ISR) ? "on" : "off");
	for (size_t off = 0; off < used; off += 32) {
		size_t n = MIN(32, used - off);

		for (size_t i = 0; i < n; i++) {
			hex[2 * i] = "0123456789abcdef"[ram_tracing[off + i] >> 4];
			hex[2 * i + 1] = "0123456789abcdef"[ram_tracing[off + i] & 0xf];
		}
		hex[2 * n] = '\0';
		crc = dump_line(crc, "ctf,%05x,%s\n", (unsigned int)off, hex);
	}
	dump_crc(crc);
}
#endif

void tracer_start(void)
{
#if defined(CONFIG_TRACING_USER)
	trace_start();
#elif defined(CONFIG_TRACING_CTF)
	ctf_cmd("enable");
#endif
}

void tracer_stop(void)
{
#if defined(CONFIG_TRACING_USER)
	trace_stop();
#elif defined(CONFIG_TRACING_CTF)
	ctf_cmd("disable");
#endif
}

void tracer_dump(void)
{
#if defined(CONFIG_TRACING_USER)
	trace_dump();
#elif defined(CONFIG_TRACING_CTF)
	ctf_dump();
#endif
}

#if defined(CONFIG_TRACING)
/* Defined by both tracing_user.c and ctf_top.c. */
void sys_trace_k_thread_switched_in(void);

#define BENCH_CALLS 64

static int cmp_u32(const void *a, const void *b)
{
	uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;

	return (x > y) - (x < y);
}
#endif

void tracer_bench(void)
{
#if defined(CONFIG_TRACING)
	uint32_t d[BENCH_CALLS], empty = UINT32_MAX;
	unsigned int key;

	tracer_start();
	key = irq_lock();
	/* Cost of the two cycle reads themselves, to subtract. */
	for (int i = 0; i < 8; i++) {
		uint32_t t0 = k_cycle_get_32();

		empty = MIN(empty, k_cycle_get_32() - t0);
	}
	for (int i = 0; i < BENCH_CALLS; i++) {
		uint32_t t0 = k_cycle_get_32();

		sys_trace_k_thread_switched_in();
		d[i] = k_cycle_get_32() - t0 - empty;
	}
	irq_unlock(key);
	tracer_stop();

	qsort(d, BENCH_CALLS, sizeof(d[0]), cmp_u32);
	printk("# bench: tracer %s, switched_in hook cycles: min %u, median %u, max %u "
	       "(%d calls, IRQs locked, cycle-read cost %u subtracted)\n", tracer_name, d[0],
	       d[BENCH_CALLS / 2], d[BENCH_CALLS - 1], BENCH_CALLS, empty);
#else
	printk("# bench: tracer none\n");
#endif
}
