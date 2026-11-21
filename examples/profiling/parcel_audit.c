#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <papi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

enum { DEPOTS = 24, ZONES = 12, DAYS = 14, SERVICES = 3, WEIGHT_BANDS = 16 };

typedef struct {
    uint32_t tracking_id;
    unsigned zone;
    unsigned grams;
    unsigned service;
    unsigned scans;
    unsigned depot;
    unsigned status;
} Parcel;

typedef struct {
    uint32_t tracking_id;
    unsigned depot;
    unsigned day;
    unsigned status;
} Scan;

typedef struct {
    unsigned zone;
    unsigned service;
    unsigned max_grams;
    unsigned cents;
} Tariff;

typedef struct {
    unsigned depot;
    unsigned day;
    unsigned status;
    unsigned cents;
} Receipt;

static uint32_t draw(uint32_t *state)
{
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    return *state = x;
}

static unsigned parse_count(const char *arg, unsigned max)
{
    char *end;
    errno = 0;
    unsigned long value = strtoul(arg, &end, 10);
    if (*arg < '0' || *arg > '9' || *end || errno || value < 1 || value > max) {
        fprintf(stderr, "expected an integer from 1 to %u: %s\n", max, arg);
        exit(EXIT_FAILURE);
    }
    return (unsigned)value;
}

static void *allocate(size_t count, size_t size)
{
    void *memory = calloc(count, size);
    if (memory == NULL) {
        perror("calloc");
        exit(EXIT_FAILURE);
    }
    return memory;
}

static Parcel *find_parcel(Parcel *parcels, size_t count, uint32_t tracking_id)
{
    for (size_t i = 0; i < count; ++i) {
        if (parcels[i].tracking_id == tracking_id)
            return &parcels[i];
    }
    return NULL;
}

static unsigned quote(const Tariff *tariffs, size_t count, const Parcel *parcel)
{
    for (size_t i = 0; i < count; ++i) {
        const Tariff *rule = &tariffs[i];
        if (rule->zone == parcel->zone && rule->service == parcel->service &&
            parcel->grams <= rule->max_grams)
            return rule->cents;
    }
    return 0;
}

static void replay(Parcel *parcels, size_t parcel_count, const Scan *scans, size_t scan_count,
                   const Tariff *tariffs, size_t tariff_count, Receipt *receipts)
{
    for (size_t i = 0; i < scan_count; ++i) {
        const Scan *scan = &scans[i];
        Parcel *parcel = find_parcel(parcels, parcel_count, scan->tracking_id);
        unsigned cents = 0;
        if (parcel != NULL) {
            if (parcel->scans == 0 || parcel->depot != scan->depot)
                cents = quote(tariffs, tariff_count, parcel);
            parcel->scans++;
            parcel->depot = scan->depot;
            parcel->status = scan->status;
        }
        receipts[i] = (Receipt){scan->depot, scan->day, parcel == NULL ? 0 : scan->status, cents};
    }
}

static uint64_t summarize(const Receipt *receipts, size_t count,
                          const Parcel *parcels, size_t parcel_count)
{
    uint64_t digest = 0;
    for (unsigned depot = 0; depot < DEPOTS; ++depot) {
        for (unsigned day = 0; day < DAYS; ++day) {
            uint64_t charges = 0;
            unsigned delivered = 0, rerouted = 0, unmatched = 0;
            for (size_t i = 0; i < count; ++i) {
                const Receipt *receipt = &receipts[i];
                if (receipt->depot != depot || receipt->day != day)
                    continue;
                charges += receipt->cents;
                delivered += receipt->status == 3;
                rerouted += receipt->status == 2;
                unmatched += receipt->status == 0;
            }
            digest = digest * 1099511628211ULL ^ (charges + 7 * delivered +
                                                  13 * rerouted + 19 * unmatched);
        }
        for (size_t i = 0; i < parcel_count; ++i) {
            if (parcels[i].scans && parcels[i].depot == depot)
                digest = digest * 1099511628211ULL ^ (parcels[i].tracking_id + parcels[i].status);
        }
    }
    return digest;
}

static void check_papi(int status, const char *region)
{
    if (status != PAPI_OK) {
        fprintf(stderr, "PAPI %s failed: %s\n", region, PAPI_strerror(status));
        exit(EXIT_FAILURE);
    }
}

static uint64_t monotonic_ns(void)
{
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time) != 0) {
        perror("clock_gettime");
        exit(EXIT_FAILURE);
    }
    return (uint64_t)time.tv_sec * 1000000000ULL + (uint64_t)time.tv_nsec;
}

int main(int argc, char **argv)
{
    if (argc > 4) {
        fprintf(stderr, "usage: %s [parcels [scans [repetitions]]]\n", argv[0]);
        return EXIT_FAILURE;
    }
    unsigned parcel_count = argc > 1 ? parse_count(argv[1], 100000) : 8000;
    unsigned scan_count = argc > 2 ? parse_count(argv[2], 1000000) : 100000;
    unsigned repetitions = argc > 3 ? parse_count(argv[3], 100) : 5;
    Parcel *inventory = allocate(parcel_count, sizeof(*inventory));
    Parcel *parcels = allocate(parcel_count, sizeof(*parcels));
    Scan *scans = allocate(scan_count, sizeof(*scans));
    Receipt *receipts = allocate(scan_count, sizeof(*receipts));
    Tariff tariffs[ZONES * WEIGHT_BANDS * SERVICES];
    size_t tariff_count = 0;
    uint32_t state = 0x615aa312;

    for (unsigned band = 1; band <= WEIGHT_BANDS; ++band) {
        for (unsigned zone = 0; zone < ZONES; ++zone) {
            for (unsigned service = 0; service < SERVICES; ++service) {
                tariffs[tariff_count++] = (Tariff){
                    zone, service, band * 500, 130 + 37 * zone + 81 * service + 24 * band,
                };
            }
        }
    }
    for (unsigned i = 0; i < parcel_count; ++i) {
        inventory[i].tracking_id = 1009 + 7919 * i;
        inventory[i].zone = draw(&state) % ZONES;
        inventory[i].grams = 1 + draw(&state) % (WEIGHT_BANDS * 500);
        inventory[i].service = draw(&state) % SERVICES;
    }
    for (unsigned i = 0; i < scan_count; ++i) {
        unsigned range = (i % 5) ? (parcel_count + 7) / 8 : parcel_count;
        unsigned position = (parcel_count / 3 + draw(&state) % range) % parcel_count;
        scans[i].tracking_id = i % 97 ? inventory[position].tracking_id : UINT32_MAX;
        scans[i].depot = draw(&state) % DEPOTS;
        scans[i].day = (unsigned)((uint64_t)i * DAYS / scan_count);
        scans[i].status = 1 + draw(&state) % 3;
    }

    uint64_t checksum = 0;
    printf("parcels: %u\nscans: %u\n", parcel_count, scan_count);
    for (unsigned run = 0; run < repetitions; ++run) {
        memcpy(parcels, inventory, parcel_count * sizeof(*parcels));
        check_papi(PAPI_hl_region_begin("scan_replay"), "scan_replay begin");
        uint64_t start = monotonic_ns();
        replay(parcels, parcel_count, scans, scan_count, tariffs, tariff_count, receipts);
        check_papi(PAPI_hl_region_end("scan_replay"), "scan_replay end");

        check_papi(PAPI_hl_region_begin("depot_report"), "depot_report begin");
        uint64_t digest = summarize(receipts, scan_count, parcels, parcel_count);
        uint64_t elapsed = monotonic_ns() - start;
        check_papi(PAPI_hl_region_end("depot_report"), "depot_report end");
        if (run == 0)
            checksum = digest;
        else if (digest != checksum) {
            fputs("parcel audit produced inconsistent reports\n", stderr);
            return EXIT_FAILURE;
        }
        printf("BenchmarkParcelAudit 1 %" PRIu64 " ns/op\n", elapsed);
    }
    fprintf(stderr, "parcel audit checksum: %016" PRIx64 "\n", checksum);
    free(receipts);
    free(scans);
    free(parcels);
    free(inventory);
    return EXIT_SUCCESS;
}
