#include <zephyr/kernel.h>

#include "burn.h"

/*
 * k_busy_wait() measures wall-clock time: if the thread is preempted for
 * 20 ms during a 150 ms busy-wait, it only gets 130 ms of real work done.
 * Asking the scheduler how many cycles this thread has actually run fixes that.
 */
void burn_cpu_ms(uint32_t ms)
{
	k_thread_runtime_stats_t stats;
	uint64_t end;

	k_thread_runtime_stats_get(k_current_get(), &stats);
	end = stats.execution_cycles + k_ms_to_cyc_ceil64(ms);

	do {
		k_thread_runtime_stats_get(k_current_get(), &stats);
	} while (stats.execution_cycles < end);
}
