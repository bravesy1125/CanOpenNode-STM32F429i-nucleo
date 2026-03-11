/*
 * Domain variable binding for OD entry 0x2004:00.
 *
 * The generated OD entry has no built-in backing store, so we install read/write
 * callbacks and provide dedicated RAM storage here.
 *
 * Why this file exists:
 * - OD entry 0x2004:00 is DOMAIN type and uses custom callbacks.
 * - SDO server calls read/write repeatedly with chunked payload.
 * - This file tracks current valid length and enforces bounds.
 */

#include "domains.h"

#include <string.h>

#include "CANopen.h"
#include "OD.h"

/* Maximum RAM reserved for the 0x2004:00 DOMAIN object. */
#define TESTDOMAIN_SIZE 4096U

typedef struct {
    /* Start address of backing storage for one DOMAIN variable. */
    uint8_t* data;
    /* Total capacity of backing storage, in bytes. */
    OD_size_t size;
    /*
     * Number of valid bytes currently stored in "data".
     * Always <= size.
     */
    OD_size_t dataLength;
} OD_domainBuffer_t;

/* Dedicated storage for OD entry 0x2004:00. */
static uint8_t testDomainBuffer[TESTDOMAIN_SIZE];
/* Context object passed back to callbacks through testDomainExtension.object. */
static OD_domainBuffer_t testDomain = {
    .data = testDomainBuffer,
    .size = TESTDOMAIN_SIZE,
    .dataLength = 0U,
};
/* Runtime OD extension installed on OD entry 0x2004:00. */
static OD_extension_t testDomainExtension;

/* Shared guard used by both read and write callbacks. */
static bool_t
domain_stream_invalid(const OD_stream_t* stream) {
    return (stream == NULL) || (stream->object == NULL);
}

static ODR_t
OD_read_testDomain(OD_stream_t* stream, void* buf, OD_size_t count, OD_size_t* countRead) {
    /* Validate callback inputs. For count == 0, buf may legitimately be NULL. */
    if (domain_stream_invalid(stream) || (countRead == NULL) || ((count > 0U) && (buf == NULL))) {
        return ODR_DEV_INCOMPAT;
    }

    OD_domainBuffer_t* domain = stream->object;
    /* Default output for all early-return paths. */
    *countRead = 0U;

    /* dataOffset must always point inside current valid data window. */
    if (stream->dataOffset > domain->dataLength) {
        return ODR_DEV_INCOMPAT;
    }

    /* Remaining readable bytes from current offset. */
    OD_size_t remaining = domain->dataLength - stream->dataOffset;
    if (remaining == 0U) {
        /* End of object reached: reset offset for the next upload session. */
        stream->dataOffset = 0U;
        return ODR_OK;
    }

    if (count == 0U) {
        /* Caller provided no destination space this round. */
        return ODR_PARTIAL;
    }

    /* Copy one chunk, bounded by caller buffer and remaining valid bytes. */
    OD_size_t toCopy = (remaining > count) ? count : remaining;
    (void)memcpy(buf, &domain->data[stream->dataOffset], toCopy);
    *countRead = toCopy;

    if (toCopy < remaining) {
        /* More data pending for subsequent callback invocation. */
        stream->dataOffset += toCopy;
        return ODR_PARTIAL;
    }

    /* Upload complete for this object access. */
    stream->dataOffset = 0U;
    return ODR_OK;
}

static ODR_t
OD_write_testDomain(OD_stream_t* stream, const void* buf, OD_size_t count, OD_size_t* countWritten) {
    /* Validate callback inputs. For count == 0, buf may legitimately be NULL. */
    if (domain_stream_invalid(stream) || (countWritten == NULL) || ((count > 0U) && (buf == NULL))) {
        return ODR_DEV_INCOMPAT;
    }

    OD_domainBuffer_t* domain = stream->object;
    /* Default output for all early-return paths. */
    *countWritten = 0U;

    /* dataOffset must always point inside reserved buffer range. */
    if (stream->dataOffset > domain->size) {
        return ODR_DEV_INCOMPAT;
    }

    /*
     * If SDO has already resolved a concrete total length (stream->dataLength > 0),
     * reject it early if it exceeds buffer capacity.
     */
    if ((stream->dataLength > 0U) && (stream->dataLength > domain->size)) {
        return ODR_DATA_LONG;
    }

    /* Per-chunk bound check. */
    OD_size_t remaining = domain->size - stream->dataOffset;
    if (count > remaining) {
        return ODR_DATA_LONG;
    }

    /* Store incoming chunk into backing RAM. */
    if (count > 0U) {
        (void)memcpy(&domain->data[stream->dataOffset], buf, count);
    }
    *countWritten = count;
    stream->dataOffset += count;

    /*
     * Track the farthest written position as current valid payload length.
     * Equivalent to: dataLength = max(dataLength, dataOffset).
     */
    if (domain->dataLength < stream->dataOffset) {
        domain->dataLength = stream->dataOffset;
    }

    /*
     * Unknown total length:
     * Some masters don't indicate total size at download initiate.
     * SDO server will keep feeding chunks and finalize later.
     */
    if (stream->dataLength == 0U) {
        return ODR_PARTIAL;
    }

    /* Known total length but not complete yet. */
    if (stream->dataOffset < stream->dataLength) {
        return ODR_PARTIAL;
    }

    /* Received more than expected total size. */
    if (stream->dataOffset > stream->dataLength) {
        return ODR_DATA_LONG;
    }

    /* Exact end reached: finalize and reset offset for the next transfer. */
    domain->dataLength = stream->dataLength;
    stream->dataOffset = 0U;
    return ODR_OK;
}

void
domains_init(void) {
    /* Register callbacks for OD entry 0x2004:00 (DOMAIN). */
    (void)memset(&testDomainExtension, 0, sizeof(testDomainExtension));
    testDomainExtension.object = &testDomain;
    testDomainExtension.read = OD_read_testDomain;
    testDomainExtension.write = OD_write_testDomain;
    (void)OD_extension_init(OD_ENTRY_H2004_testdomain, &testDomainExtension);
}
