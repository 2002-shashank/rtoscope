cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(rtoscope)

target_sources(app PRIVATE src/burn.c src/trace.c src/monitor.c)
target_sources_ifdef(CONFIG_RTOSCOPE_WDT app PRIVATE src/watchdog.c)
target_sources_ifdef(CONFIG_RTOSCOPE_DEMO_PERIODIC app PRIVATE src/periodic.c)
target_sources_ifdef(CONFIG_RTOSCOPE_DEMO_INVERSION app PRIVATE src/inversion.c)
