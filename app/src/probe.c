#include <string.h>

#include <zephyr/kernel.h>

#include "dump.h"
#include "probe.h"

#define PROBE_TASKS 3
#define PROBE_JOBS  64   /* H has 30 jobs per 3 s hyperperiod */

static uint32_t wake[PROBE_TASKS][PROBE_JOBS];
static uint32_t done[PROBE_TASKS][PROBE_JOBS];
static int last_job[PROBE_TASKS];

void probe_reset(void)
{
	memset(wake, 0, sizeof(wake));
	memset(done, 0, sizeof(done));
	memset(last_job, 0, sizeof(last_job));
}

void probe_wake(int task, int job)
{
	if (task < PROBE_TASKS && job >= 1 && job <= PROBE_JOBS) {
		wake[task][job - 1] = k_cycle_get_32();
	}
}

void probe_done(int task, int job)
{
	if (task < PROBE_TASKS && job >= 1 && job <= PROBE_JOBS) {
		done[task][job - 1] = k_cycle_get_32();
		last_job[task] = MAX(last_job[task], job);
	}
}

void probe_dump(const char *const names[], int ntasks)
{
	uint32_t t0 = wake[0][0];   /* H's first wake: the first release */
	uint32_t crc = 0;
	int jobs = 0;

	for (int t = 0; t < ntasks && t < PROBE_TASKS; t++) {
		jobs += last_job[t];
	}
	crc = dump_line(crc, "# rtoscope probe, cycle clock %u Hz, %d jobs\n",
			sys_clock_hw_cycles_per_sec(), jobs);
	crc = dump_line(crc, "task,job,wake,done\n");
	for (int t = 0; t < ntasks && t < PROBE_TASKS; t++) {
		for (int j = 0; j < last_job[t]; j++) {
			/* Unsigned subtraction stays correct across one CCOUNT wrap. */
			crc = dump_line(crc, "%s,%d,%u,%u\n", names[t], j + 1,
					wake[t][j] - t0, done[t][j] - t0);
		}
	}
	dump_crc(crc);
}
