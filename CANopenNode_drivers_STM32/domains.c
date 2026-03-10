/*
 * This file is dedicated to Object Dictionary domain variables.
 *
 * Generated OD.c/OD.h may define a domain object without assigning backing memory.
 * In that case the domain entry must be connected with custom OD extension callbacks
 * from this file.
 *
 * Each domain variable must have its own dedicated storage buffer. If you add more
 * OD domain objects later, define a separate buffer, callback context and binding
 * for each one.
 *
 * In this project, OD entry 0x2004:00 is handled here.
 *
 * Data flow is:
 * 1. CANopenNode receives an SDO read/write request for a domain object.
 * 2. OD_getSub() resolves the OD entry.
 * 3. Because domains_init() installs an OD extension on that entry, CANopenNode
 *    calls the custom read/write callbacks from this file instead of accessing
 *    generated OD memory directly.
 * 4. Those callbacks operate on the dedicated buffer assigned to that domain.
 *
 * Important design rule:
 * One OD domain variable should have its own dedicated backing storage.
 * If you later add another domain object, create another buffer/context/binding
 * rather than reusing the same storage implicitly.
 */

 #include "domains.h"

#include <string.h>

#include "CANopen.h"
#include "OD.h"

#define TESTDOMAIN_SIZE 4096U

typedef struct {
    /* Start address of the memory assigned to one OD domain variable. */
    uint8_t* data;
    /* Total capacity of that memory area, in bytes. */
    OD_size_t size;
} OD_domainBuffer_t;

/* Dedicated storage for OD entry 0x2004:00. */
static uint8_t testDomainBuffer[TESTDOMAIN_SIZE];
/* Context object passed back to callbacks through testDomainExtension.object. */
static OD_domainBuffer_t testDomain = {
    .data = testDomainBuffer,
    .size = TESTDOMAIN_SIZE,
};
/* Runtime OD extension installed on OD entry 0x2004:00. */
static OD_extension_t testDomainExtension;

static ODR_t
OD_read_testDomain(OD_stream_t* stream, void* buf, OD_size_t count, OD_size_t* countRead) {
    /*
     * Read callback used during SDO upload of the domain object.
     *
     * stream->object    : points to the OD_domainBuffer_t configured in domains_init()
     * stream->dataOffset: current byte offset maintained by CANopenNode
     * buf/count         : destination buffer and requested chunk size
     * countRead         : number of bytes actually returned to CANopenNode
     *
     * If the whole domain does not fit in one callback call, return ODR_PARTIAL.
     * CANopenNode will call us again and continue from the updated dataOffset.
     */
    if ((stream == NULL) || (buf == NULL) || (countRead == NULL) || (stream->object == NULL)) {
        return ODR_DEV_INCOMPAT;
    }

    OD_domainBuffer_t* domain = stream->object;
    if (stream->dataOffset >= domain->size) {
        return ODR_DEV_INCOMPAT;
    }

    OD_size_t remaining = domain->size - stream->dataOffset;
    OD_size_t toCopy = (remaining > count) ? count : remaining;

    /* Copy the next readable chunk from the dedicated backing buffer. */
    (void)memcpy(buf, &domain->data[stream->dataOffset], toCopy);
    *countRead = toCopy;

    if (toCopy < remaining) {
        /* Not finished yet, remember the new offset for the next callback call. */
        stream->dataOffset += toCopy;
        return ODR_PARTIAL;
    }

    /* Transfer finished, reset the offset so the next upload starts from byte 0. */
    stream->dataOffset = 0U;
    return ODR_OK;
}

static ODR_t
OD_write_testDomain(OD_stream_t* stream, const void* buf, OD_size_t count, OD_size_t* countWritten) {
    /*
     * Write callback used during SDO download into the domain object.
     *
     * CANopenNode may deliver the payload in multiple chunks. We place each chunk
     * into the dedicated backing buffer using stream->dataOffset as the write pointer.
     * Returning ODR_PARTIAL tells CANopenNode that the transfer should continue.
     */
    if ((stream == NULL) || (buf == NULL) || (countWritten == NULL) || (stream->object == NULL)) {
        return ODR_DEV_INCOMPAT;
    }

    OD_domainBuffer_t* domain = stream->object;
    if (stream->dataOffset >= domain->size) {
        return ODR_DEV_INCOMPAT;
    }

    OD_size_t remaining = domain->size - stream->dataOffset;
    if (count > remaining) {
        /* Reject any download chunk that would overflow the assigned buffer. */
        return ODR_DATA_LONG;
    }

    /* Copy the received chunk into the assigned backing storage. */
    (void)memcpy(&domain->data[stream->dataOffset], buf, count);
    *countWritten = count;

    if (count < remaining) {
        /* More chunks are expected, so preserve the current write position. */
        stream->dataOffset += count;
        return ODR_PARTIAL;
    }

    /* Whole transfer completed, reset offset for the next download. */
    stream->dataOffset = 0U;
    return ODR_OK;
}

void
domains_init(void) {
    /*
     * Bind OD entry 0x2004:00 to its dedicated external domain storage.
     *
     * After this registration, SDO access to 0x2004:00 is routed through
     * OD_read_testDomain()/OD_write_testDomain() and uses testDomainBuffer as the
     * real payload storage.
     */
    (void)memset(&testDomainExtension, 0, sizeof(testDomainExtension));
    testDomainExtension.object = &testDomain;
    testDomainExtension.read = OD_read_testDomain;
    testDomainExtension.write = OD_write_testDomain;
    (void)OD_extension_init(OD_ENTRY_H2004_testdomain, &testDomainExtension);
}
