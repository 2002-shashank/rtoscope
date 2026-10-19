#include <stdarg.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/printk.h>

#include "dump.h"

uint32_t dump_line(uint32_t crc, const char *fmt, ...)
{
	char line[128];
	va_list ap;
	int len;

	va_start(ap, fmt);
	len = vsnprintk(line, sizeof(line), fmt, ap);
	va_end(ap);
	len = MIN(len, (int)sizeof(line) - 1);

	printk("%s", line);
	return crc32_ieee_update(crc, (const uint8_t *)line, len);
}

void dump_crc(uint32_t crc)
{
	printk("# crc32 %08x\n", crc);
}
