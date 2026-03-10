#include "domains.h"

#include <string.h>

#include "CANopen.h"
#include "OD.h"

#define TESTDOMAIN_SIZE 4096U

typedef struct {
    uint8_t* data;
    OD_size_t size;
} OD_domainBuffer_t;

static uint8_t testDomainBuffer[TESTDOMAIN_SIZE];
static OD_domainBuffer_t testDomain = {
    .data = testDomainBuffer,
    .size = TESTDOMAIN_SIZE,
};
static OD_extension_t testDomainExtension;

static ODR_t
OD_read_testDomain(OD_stream_t* stream, void* buf, OD_size_t count, OD_size_t* countRead) {
    if ((stream == NULL) || (buf == NULL) || (countRead == NULL) || (stream->object == NULL)) {
        return ODR_DEV_INCOMPAT;
    }

    OD_domainBuffer_t* domain = stream->object;
    if (stream->dataOffset >= domain->size) {
        return ODR_DEV_INCOMPAT;
    }

    OD_size_t remaining = domain->size - stream->dataOffset;
    OD_size_t toCopy = (remaining > count) ? count : remaining;

    (void)memcpy(buf, &domain->data[stream->dataOffset], toCopy);
    *countRead = toCopy;

    if (toCopy < remaining) {
        stream->dataOffset += toCopy;
        return ODR_PARTIAL;
    }

    stream->dataOffset = 0U;
    return ODR_OK;
}

static ODR_t
OD_write_testDomain(OD_stream_t* stream, const void* buf, OD_size_t count, OD_size_t* countWritten) {
    if ((stream == NULL) || (buf == NULL) || (countWritten == NULL) || (stream->object == NULL)) {
        return ODR_DEV_INCOMPAT;
    }

    OD_domainBuffer_t* domain = stream->object;
    if (stream->dataOffset >= domain->size) {
        return ODR_DEV_INCOMPAT;
    }

    OD_size_t remaining = domain->size - stream->dataOffset;
    if (count > remaining) {
        return ODR_DATA_LONG;
    }

    (void)memcpy(&domain->data[stream->dataOffset], buf, count);
    *countWritten = count;

    if (count < remaining) {
        stream->dataOffset += count;
        return ODR_PARTIAL;
    }

    stream->dataOffset = 0U;
    return ODR_OK;
}

void
domains_init(void) {
    (void)memset(&testDomainExtension, 0, sizeof(testDomainExtension));
    testDomainExtension.object = &testDomain;
    testDomainExtension.read = OD_read_testDomain;
    testDomainExtension.write = OD_write_testDomain;
    (void)OD_extension_init(OD_ENTRY_H2004_testdomain, &testDomainExtension);
}
