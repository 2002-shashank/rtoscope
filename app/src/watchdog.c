/*
 * Watchdog layer for the periodic demo (phase 2, step 4).
 *
 * Two levels:
 *   task_wdt  one software channel per thread. A thread that stops making
 *             progress stops feeding its own channel, so the expiry names
 *             the culprit, even while other threads (which a single global
 *             watchdog fed from a timer would trust) still run.
 *   TIMG0     hardware watchdog, fed by task_wdt's own timer. If interrupts
 *             are locked, that timer can't run either, and TIMG0 resets the
 *             chip (stage 0: interrupt, stage 1: system reset).
 *
 * task_wdt's expiry calls sys_reboot(), a *software* reset, so the reset
 * cause alone can't tell it from any other reboot: the last words in RTC
 * memory (kept across resets, lost on power-on) say what happened.
 */

#include <string.h>

#include <zephyr/device.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/task_wdt/task_wdt.h>

#include "watchdog.h"

#define LW_MAGIC 0x52544f53u  /* "RTOS" */

struct last_words {
	uint32_t magic;
	uint32_t fault_ms;    /* uptime when a test fault was injected, or 0 */
	uint32_t fired_ms;    /* uptime when a task watchdog channel expired, or 0 */
	char fault[12];
	char channel[8];      /* thread whose channel expired */
	char running[8];      /* thread interrupted by the expiry */
};

/* NOLOAD section in RTC slow memory (soc/espressif/esp32/default.ld). */
static struct last_words lw __attribute__((section(".rtc_noinit")));

static void (*report_hook)(void);

static void copy_name(char *dst, size_t len, const char *src)
{
	strncpy(dst, src != NULL ? src : "?", len - 1);
	dst[len - 1] = '\0';
}

bool watchdog_boot_report(void)
{
	uint32_t cause = 0;
	bool recovered = false;

	(void)hwinfo_get_reset_cause(&cause);
	(void)hwinfo_clear_reset_cause();
	printk("# boot: reset cause 0x%x%s%s%s%s\n", cause,
	       (cause & RESET_POR) ? " power-on" : "", (cause & RESET_PIN) ? " pin" : "",
	       (cause & RESET_SOFTWARE) ? " software" : "",
	       (cause & RESET_WATCHDOG) ? " watchdog" : "");

	/* After power-on (or the EN pin), RTC memory holds garbage. */
	if (lw.magic == LW_MAGIC && !(cause & (RESET_POR | RESET_PIN))) {
		recovered = true;
		if (lw.fault_ms != 0) {
			printk("# boot: last words: fault \"%s\" injected at %u ms\n",
			       lw.fault, lw.fault_ms);
		}
		if (lw.fired_ms != 0) {
			printk("# boot: last words: task watchdog channel %s expired at %u ms, "
			       "%s running\n", lw.channel, lw.fired_ms, lw.running);
		}
	}
	memset(&lw, 0, sizeof(lw));
	return recovered;
}

int watchdog_start(void)
{
	const struct device *hw = DEVICE_DT_GET_OR_NULL(DT_ALIAS(watchdog0));

	if (hw != NULL && !device_is_ready(hw)) {
		hw = NULL;
	}
	return task_wdt_init(hw);
}

/* k_timer expiry: ISR context. */
static void expired(int channel_id, void *user_data)
{
	ARG_UNUSED(channel_id);

	copy_name(lw.channel, sizeof(lw.channel), user_data);
	copy_name(lw.running, sizeof(lw.running), k_thread_name_get(k_current_get()));
	lw.fired_ms = k_uptime_get_32();
	lw.magic = LW_MAGIC;

	/* The system is going down anyway: printing here is fine. */
	printk("# wdt: task watchdog channel %s expired at %u ms, %s running\n",
	       lw.channel, lw.fired_ms, lw.running);
	if (report_hook != NULL) {
		report_hook();
	}
	sys_reboot(SYS_REBOOT_COLD);
}

int watchdog_add(const char *name, uint32_t timeout_ms)
{
	return task_wdt_add(timeout_ms, expired, (void *)name);
}

void watchdog_feed(int channel)
{
	(void)task_wdt_feed(channel);
}

void watchdog_remove(int channel)
{
	(void)task_wdt_delete(channel);
}

void watchdog_note_fault(const char *what)
{
	copy_name(lw.fault, sizeof(lw.fault), what);
	lw.fault_ms = k_uptime_get_32();
	lw.magic = LW_MAGIC;
}

void watchdog_set_report_hook(void (*hook)(void))
{
	report_hook = hook;
}
