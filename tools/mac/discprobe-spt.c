/*
 * discprobe-spt — the macOS SCSI pass-through helper for discprobe.
 *
 * macOS has no IOCTL_SCSI_PASS_THROUGH, so a bare CDB can only be sent through
 * IOKit's SCSITaskLib: IOCreatePlugInInterfaceForService() on the block storage
 * device hands out an SCSITaskDeviceInterface, and every SCSITaskInterface it
 * creates takes one CDB. That requires a little Objective-C/CF plumbing and an
 * exclusive-access handshake, which is what this helper does.
 *
 * The helper is deliberately dumb: it owns the device, executes CDBs, and stamps
 * each one with mach_absolute_time() immediately before and after the synchronous
 * task. All measurement logic stays in Python, where the analysis is validated
 * against the simulator.
 *
 * Protocol (little endian, both directions, no stdio buffering):
 *
 *   request, 28 bytes
 *     u8  command        0 = execute, 1 = quit
 *     u8  cdb_length
 *     u8  direction      0 = no data, 1 = out, 2 = in
 *     u8  pad
 *     u32 data_length
 *     u32 timeout_ms
 *     u8  cdb[16]
 *
 *   response, 32 bytes then data_length bytes when direction = 2
 *     i32 io_return
 *     u8  task_status
 *     u8  sense_key
 *     u8  sense_asc
 *     u8  sense_ascq
 *     u64 t_start_ns
 *     u64 t_end_ns
 *     u32 transferred
 *     u32 reserved
 *
 * Commands:
 *   discprobe-spt devices                 print the optical drives as JSON
 *   discprobe-spt serve [--device diskN]  speak the protocol above on stdin/stdout
 *
 * Safety: only reads are allowed through. The opcode allowlist is the same one
 * discprobe/scsi.py enforces, and it is checked here as well so a bad request
 * cannot reach the drive by any path.
 *
 * Build: tools/mac/build.sh (needs the Xcode command line tools).
 */

#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/scsi/SCSITaskLib.h>
#include <DiskArbitration/DiskArbitration.h>
#include <mach/mach_time.h>

#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MAX_DEVS 16
#define MAX_TRANSFER (128 * 1024)
#define SENSE_BYTES 32

/* ---------------------------------------------------------------- opcodes */

static const struct { uint8_t opcode; const char *name; } ALLOWED[] = {
    {0x00, "TEST UNIT READY"},
    {0x12, "INQUIRY"},
    {0x25, "READ CAPACITY"},
    {0x28, "READ(10)"},
    {0x43, "READ TOC/PMA/ATIP"},
    {0x46, "GET CONFIGURATION"},
    {0x51, "READ DISC INFORMATION"},
    {0x52, "READ TRACK INFORMATION"},
    {0x5A, "MODE SENSE(10)"},
    {0xA8, "READ(12)"},
    {0xAC, "GET PERFORMANCE"},
    {0xAD, "READ DISC STRUCTURE"},
    {0xBB, "SET CD SPEED"},
    {0xBE, "READ CD"},
};

static const char *opcode_name(uint8_t opcode)
{
    for (size_t i = 0; i < sizeof(ALLOWED) / sizeof(ALLOWED[0]); i++) {
        if (ALLOWED[i].opcode == opcode) return ALLOWED[i].name;
    }
    return NULL;
}

/* ---------------------------------------------------------------- timing */

static mach_timebase_info_data_t g_timebase;

static void timebase_init(void)
{
    if (mach_timebase_info(&g_timebase) != KERN_SUCCESS || g_timebase.denom == 0) {
        g_timebase.numer = 1;
        g_timebase.denom = 1;
    }
}

/* mach_absolute_time() ticks to nanoseconds; 128-bit intermediate avoids overflow. */
static uint64_t now_ns(void)
{
    uint64_t ticks = mach_absolute_time();
    return (uint64_t)(((__uint128_t)ticks * g_timebase.numer) / g_timebase.denom);
}

/* ---------------------------------------------------------------- device discovery */

typedef struct {
    io_service_t service;      /* retained */
    char klass[64];
    char bsd[64];
    char vendor[64];
    char product[64];
    char revision[64];
} dev_entry;

static dev_entry g_devs[MAX_DEVS];
static int g_ndev = 0;

static const char *OPTICAL_CLASSES[] = {
    "IODVDServices", "IOCompactDiscServices", "IOBDServices", "IOSCSIPeripheralDeviceNub"
};

static void copy_prop_string(io_registry_entry_t entry, CFStringRef key, char *out, size_t n)
{
    out[0] = 0;
    CFTypeRef v = IORegistryEntrySearchCFProperty(entry, kIOServicePlane, key, kCFAllocatorDefault,
                                                  kIORegistryIterateRecursively |
                                                  kIORegistryIterateParents);
    if (v == NULL) return;
    if (CFGetTypeID(v) == CFStringGetTypeID()) {
        CFStringGetCString((CFStringRef)v, out, (CFIndex)n, kCFStringEncodingUTF8);
    }
    CFRelease(v);
}

/* The keys differ by layer: block storage devices publish "Vendor Name"/"Product Name",
 * while the SCSI logical unit nub below them publishes "Vendor Identification" and
 * "Product Identification". Try both, innermost registry entry first. */
static void copy_prop_any(io_registry_entry_t entry, const char *const *keys, char *out, size_t n)
{
    out[0] = 0;
    for (int i = 0; keys[i] != NULL && out[0] == 0; i++) {
        CFStringRef key = CFStringCreateWithCString(kCFAllocatorDefault, keys[i],
                                                   kCFStringEncodingUTF8);
        if (key == NULL) continue;
        copy_prop_string(entry, key, out, n);
        CFRelease(key);
    }
}

/* The BSD name lives on the IOMedia child of the storage service. */
static void find_bsd(io_registry_entry_t service, char *out, size_t n)
{
    out[0] = 0;
    io_iterator_t it = IO_OBJECT_NULL;
    if (IORegistryEntryCreateIterator(service, kIOServicePlane, kIORegistryIterateRecursively,
                                      &it) != KERN_SUCCESS) {
        return;
    }
    io_registry_entry_t entry;
    while ((entry = IOIteratorNext(it)) != IO_OBJECT_NULL) {
        if (IOObjectConformsTo(entry, "IOMedia")) {
            char name[64] = {0};
            CFTypeRef v = IORegistryEntryCreateCFProperty(entry, CFSTR("BSD Name"),
                                                          kCFAllocatorDefault, 0);
            if (v != NULL) {
                if (CFGetTypeID(v) == CFStringGetTypeID()) {
                    CFStringGetCString((CFStringRef)v, name, sizeof(name), kCFStringEncodingUTF8);
                }
                CFRelease(v);
            }
            if (name[0] != 0) {
                strncpy(out, name, n - 1);
                out[n - 1] = 0;
                IOObjectRelease(entry);
                break;
            }
        }
        IOObjectRelease(entry);
    }
    IOObjectRelease(it);
}

static int peripheral_type(io_registry_entry_t service)
{
    int type = -1;
    CFTypeRef v = IORegistryEntryCreateCFProperty(service, CFSTR("Peripheral Device Type"),
                                                  kCFAllocatorDefault, 0);
    if (v != NULL) {
        if (CFGetTypeID(v) == CFNumberGetTypeID()) CFNumberGetValue((CFNumberRef)v,
                                                                    kCFNumberIntType, &type);
        CFRelease(v);
    }
    return type;
}

static void add_service(io_service_t service)
{
    if (g_ndev >= MAX_DEVS) return;
    /* 0x00 / 0x05 / 0x07 / 0x0e are claimed by in-kernel logical unit drivers. */
    int pdt = peripheral_type(service);
    if (pdt == 0x00 || pdt == 0x05 || pdt == 0x07 || pdt == 0x0e) return;
    dev_entry probe;
    memset(&probe, 0, sizeof(probe));
    find_bsd(service, probe.bsd, sizeof(probe.bsd));
    if (probe.bsd[0] == 0) return;
    for (int i = 0; i < g_ndev; i++) {
        if (strcmp(g_devs[i].bsd, probe.bsd) == 0) return;
    }
    dev_entry *e = &g_devs[g_ndev];
    memset(e, 0, sizeof(*e));
    if (IOObjectRetain(service) != KERN_SUCCESS) return;
    e->service = service;
    io_name_t cls = {0};
    IOObjectGetClass(service, cls);
    strncpy(e->klass, cls, sizeof(e->klass) - 1);
    strncpy(e->bsd, probe.bsd, sizeof(e->bsd) - 1);
    static const char *const VENDOR_KEYS[] = {"Vendor Name", "Vendor Identification", NULL};
    static const char *const PRODUCT_KEYS[] = {"Product Name", "Product Identification", NULL};
    static const char *const REVISION_KEYS[] = {"Product Revision Level", NULL};
    copy_prop_any(service, VENDOR_KEYS, e->vendor, sizeof(e->vendor));
    copy_prop_any(service, PRODUCT_KEYS, e->product, sizeof(e->product));
    copy_prop_any(service, REVISION_KEYS, e->revision, sizeof(e->revision));
    g_ndev++;
}

static void collect_devices(void)
{
    g_ndev = 0;
    for (size_t c = 0; c < sizeof(OPTICAL_CLASSES) / sizeof(OPTICAL_CLASSES[0]); c++) {
        CFMutableDictionaryRef matching = IOServiceMatching(OPTICAL_CLASSES[c]);
        if (matching == NULL) continue;
        io_iterator_t it = IO_OBJECT_NULL;
        if (IOServiceGetMatchingServices(kIOMainPortDefault, matching, &it) != KERN_SUCCESS) continue;
        if (it == IO_OBJECT_NULL) continue;
        io_service_t service;
        while ((service = IOIteratorNext(it)) != IO_OBJECT_NULL) {
            add_service(service);
            IOObjectRelease(service);
        }
        IOObjectRelease(it);
    }
}

static void json_escape(const char *s, char *out, size_t n)
{
    size_t j = 0;
    for (size_t i = 0; s[i] && j + 2 < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') { out[j++] = '\\'; out[j++] = (char)c; }
        else if (c < 0x20) { out[j++] = ' '; }
        else out[j++] = (char)c;
    }
    out[j] = 0;
}

static int cmd_devices(void)
{
    printf("[");
    for (int i = 0; i < g_ndev; i++) {
        char v[128], p[128], r[128];
        json_escape(g_devs[i].vendor, v, sizeof(v));
        json_escape(g_devs[i].product, p, sizeof(p));
        json_escape(g_devs[i].revision, r, sizeof(r));
        printf("%s\n  {\"name\": \"%s\", \"class\": \"%s\", \"vendor\": \"%s\", "
               "\"product\": \"%s\", \"revision\": \"%s\"}",
               i ? "," : "", g_devs[i].bsd, g_devs[i].klass, v, p, r);
    }
    printf("%s]\n", g_ndev ? "\n" : "");
    return 0;
}

/* ---------------------------------------------------------------- opening */

typedef struct {
    SCSITaskDeviceInterface **dev;
    IOCFPlugInInterface **plug;
    MMCDeviceInterface **mmc;
} opened_t;

static int g_da_done = 0;
static int g_da_ok = 0;

static void da_unmount_cb(DADiskRef disk, DADissenterRef dissenter, void *ctx)
{
    (void)disk; (void)ctx;
    g_da_ok = (dissenter == NULL);
    g_da_done = 1;
}

static int unmount_bsd(const char *bsd)
{
    if (bsd[0] == 0) return -1;
    DASessionRef session = DASessionCreate(kCFAllocatorDefault);
    if (session == NULL) return -1;
    int rc = -1;
    DADiskRef disk = DADiskCreateFromBSDName(kCFAllocatorDefault, session, bsd);
    if (disk != NULL) {
        g_da_done = 0;
        g_da_ok = 0;
        DADiskUnmount(disk, kDADiskUnmountOptionDefault, da_unmount_cb, NULL);
        for (int i = 0; i < 120 && !g_da_done; i++) {
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, false);
        }
        if (g_da_done && g_da_ok) rc = 0;
        CFRelease(disk);
    }
    CFRelease(session);
    return rc;
}

static SCSITaskDeviceInterface **task_interface_of(dev_entry *e, opened_t *o)
{
    IOCFPlugInInterface **plug = NULL;
    SInt32 score = 0;

    /* Optical drives already have an in-kernel logical unit driver, so the plain
     * SCSITask factory is not published for them; the MMC user client is. */
    if (IOCreatePlugInInterfaceForService(e->service, kIOMMCDeviceUserClientTypeID,
                                          kIOCFPlugInInterfaceID, &plug, &score) == KERN_SUCCESS &&
        plug != NULL) {
        MMCDeviceInterface **mmc = NULL;
        if ((*plug)->QueryInterface(plug, CFUUIDGetUUIDBytes(kIOMMCDeviceInterfaceID),
                                     (LPVOID *)&mmc) == S_OK && mmc != NULL) {
            SCSITaskDeviceInterface **dev = (*mmc)->GetSCSITaskDeviceInterface(mmc);
            if (dev != NULL) {
                o->mmc = mmc;
                o->plug = plug;
                return dev;
            }
            (*mmc)->Release(mmc);
        }
        IODestroyPlugInInterface(plug);
        plug = NULL;
    }
    if (IOCreatePlugInInterfaceForService(e->service, kIOSCSITaskDeviceUserClientTypeID,
                                          kIOCFPlugInInterfaceID, &plug, &score) == KERN_SUCCESS &&
        plug != NULL) {
        SCSITaskDeviceInterface **dev = NULL;
        if ((*plug)->QueryInterface(plug, CFUUIDGetUUIDBytes(kIOSCSITaskDeviceInterfaceID),
                                    (LPVOID *)&dev) == S_OK && dev != NULL) {
            o->plug = plug;
            return dev;
        }
        IODestroyPlugInInterface(plug);
    }
    return NULL;
}

static int open_entry(dev_entry *e, opened_t *o)
{
    memset(o, 0, sizeof(*o));
    o->dev = task_interface_of(e, o);
    if (o->dev == NULL) {
        fprintf(stderr, "discprobe-spt: %s did not hand out a SCSITaskDeviceInterface\n", e->bsd);
        return -1;
    }
    IOReturn r = (*o->dev)->ObtainExclusiveAccess(o->dev);
    if (r != kIOReturnSuccess) {
        /* The medium may be mounted; unmount and try again. */
        unmount_bsd(e->bsd);
        r = (*o->dev)->ObtainExclusiveAccess(o->dev);
    }
    if (r != kIOReturnSuccess) {
        fprintf(stderr, "discprobe-spt: exclusive access to %s failed: 0x%08X%s\n",
                e->bsd, (unsigned)r,
                (r == kIOReturnBusy) ? " (still mounted; try `diskutil unmountDisk`)" : "");
        (*o->dev)->Release(o->dev);
        if (o->mmc) (*o->mmc)->Release(o->mmc);
        if (o->plug) IODestroyPlugInInterface(o->plug);
        memset(o, 0, sizeof(*o));
        return -1;
    }
    return 0;
}

static void close_entry(opened_t *o)
{
    if (o->dev) {
        (*o->dev)->ReleaseExclusiveAccess(o->dev);
        (*o->dev)->Release(o->dev);
    }
    if (o->mmc) (*o->mmc)->Release(o->mmc);
    if (o->plug) IODestroyPlugInInterface(o->plug);
    memset(o, 0, sizeof(*o));
}

/* ---------------------------------------------------------------- execution */

typedef struct {
    IOReturn io_return;
    SCSITaskStatus task_status;
    SCSI_Sense_Data sense;
    uint64_t t_start_ns;
    uint64_t t_end_ns;
    uint64_t transferred;
} cmd_result;

static cmd_result exec_cmd(SCSITaskDeviceInterface **dev, const uint8_t *cdb, UInt8 cdb_len,
                           void *buf, UInt64 buf_len, UInt8 direction, UInt32 timeout_ms)
{
    cmd_result r;
    memset(&r, 0, sizeof(r));
    r.io_return = kIOReturnError;

    SCSITaskInterface **task = (*dev)->CreateSCSITask(dev);
    if (task == NULL) {
        r.io_return = kIOReturnNoMemory;
        return r;
    }
    IOReturn err = (*task)->SetCommandDescriptorBlock(task, (UInt8 *)cdb, cdb_len);
    if (err == kIOReturnSuccess && buf != NULL && buf_len > 0) {
        SCSITaskSGElement element;
        element.address = (IOVirtualAddress)buf;
        element.length = buf_len;
        err = (*task)->SetScatterGatherEntries(task, &element, 1, buf_len, direction);
    }
    if (err == kIOReturnSuccess) err = (*task)->SetTimeoutDuration(task, timeout_ms);
    if (err == kIOReturnSuccess) {
        r.t_start_ns = now_ns();
        err = (*task)->ExecuteTaskSync(task, &r.sense, &r.task_status, &r.transferred);
        r.t_end_ns = now_ns();
    }
    r.io_return = err;
    (*task)->Release(task);
    return r;
}

/* ---------------------------------------------------------------- framing */

static int read_exact(void *buf, size_t n)
{
    uint8_t *p = (uint8_t *)buf;
    size_t done = 0;
    while (done < n) {
        ssize_t got = read(STDIN_FILENO, p + done, n - done);
        if (got < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (got == 0) return -1;
        done += (size_t)got;
    }
    return 0;
}

static int write_exact(const void *buf, size_t n)
{
    const uint8_t *p = (const uint8_t *)buf;
    size_t done = 0;
    while (done < n) {
        ssize_t put = write(STDOUT_FILENO, p + done, n - done);
        if (put < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        done += (size_t)put;
    }
    return 0;
}

static uint32_t rd_u32(const uint8_t *b) { return (uint32_t)b[0] | ((uint32_t)b[1] << 8) |
                                                  ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24); }

static void wr_u32(uint8_t *b, uint32_t v)
{
    b[0] = (uint8_t)(v); b[1] = (uint8_t)(v >> 8);
    b[2] = (uint8_t)(v >> 16); b[3] = (uint8_t)(v >> 24);
}

static void wr_u64(uint8_t *b, uint64_t v)
{
    wr_u32(b, (uint32_t)v);
    wr_u32(b + 4, (uint32_t)(v >> 32));
}

static int cmd_serve(dev_entry *e)
{
    opened_t o;
    if (open_entry(e, &o) != 0) return 3;
    static uint8_t buf[MAX_TRANSFER];
    fprintf(stderr, "discprobe-spt: %s ready\n", e->bsd);
    fflush(stderr);

    for (;;) {
        uint8_t req[28];
        if (read_exact(req, sizeof(req)) != 0) break;
        uint8_t command = req[0];
        if (command == 1) break;
        if (command != 0) break;
        uint8_t cdb_len = req[1] > 16 ? 16 : req[1];
        uint8_t dir = req[2];
        uint32_t data_len = rd_u32(req + 4);
        uint32_t timeout_ms = rd_u32(req + 8);
        uint8_t cdb[16];
        memcpy(cdb, req + 12, sizeof(cdb));
        if (data_len > MAX_TRANSFER) data_len = MAX_TRANSFER;
        if (timeout_ms == 0) timeout_ms = 30000;

        uint8_t resp[32];
        memset(resp, 0, sizeof(resp));
        if (cdb_len == 0 || opcode_name(cdb[0]) == NULL) {
            /* Refuse before anything reaches the device. */
            wr_u32(resp, (uint32_t)kIOReturnUnsupported);
            resp[4] = 0xFF;
            if (write_exact(resp, sizeof(resp)) != 0) break;
            continue;
        }
        if (dir == 1) {
            /* This probe never writes to the medium. */
            wr_u32(resp, (uint32_t)kIOReturnUnsupported);
            resp[4] = 0xFF;
            if (write_exact(resp, sizeof(resp)) != 0) break;
            continue;
        }

        UInt8 xfer = kSCSIDataTransfer_NoDataTransfer;
        if (dir == 2 && data_len > 0) xfer = kSCSIDataTransfer_FromTargetToInitiator;

        cmd_result r = exec_cmd(o.dev, cdb, cdb_len, (dir == 2 && data_len) ? buf : NULL,
                                data_len, xfer, timeout_ms);
        if (r.io_return != kIOReturnSuccess && r.t_end_ns == 0) {
            r.t_end_ns = now_ns();
            r.t_start_ns = r.t_end_ns;
        }
        uint32_t transferred = (dir == 2) ? (uint32_t)(r.transferred > data_len ? data_len : r.transferred) : 0;
        wr_u32(resp, (uint32_t)r.io_return);
        resp[4] = (uint8_t)r.task_status;
        resp[5] = (uint8_t)(r.sense.SENSE_KEY & 0x0F);
        resp[6] = (uint8_t)r.sense.ADDITIONAL_SENSE_CODE;
        resp[7] = (uint8_t)r.sense.ADDITIONAL_SENSE_CODE_QUALIFIER;
        wr_u64(resp + 8, r.t_start_ns);
        wr_u64(resp + 16, r.t_end_ns);
        wr_u32(resp + 24, transferred);
        if (write_exact(resp, sizeof(resp)) != 0) break;
        if (transferred > 0 && write_exact(buf, transferred) != 0) break;
    }
    close_entry(&o);
    return 0;
}

/* ---------------------------------------------------------------- main */

static int fail(int code, const char *what)
{
    fprintf(stderr, "discprobe-spt: %s\n", what);
    return code;
}

int main(int argc, char **argv)
{
    signal(SIGPIPE, SIG_IGN);
    timebase_init();
    if (argc < 2) {
        fprintf(stderr, "usage: %s devices | serve [--device diskN]\n", argv[0]);
        return 2;
    }
    collect_devices();

    if (strcmp(argv[1], "devices") == 0) return cmd_devices();

    if (strcmp(argv[1], "serve") != 0) return fail(2, "unknown command");

    const char *want = NULL;
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--device") == 0 && i + 1 < argc) want = argv[++i];
        else return fail(2, "unknown option");
    }
    dev_entry *pick = NULL;
    for (int i = 0; i < g_ndev; i++) {
        if (want == NULL || strcmp(g_devs[i].bsd, want) == 0) { pick = &g_devs[i]; break; }
    }
    if (pick == NULL) {
        if (g_ndev == 0) return fail(4, "no optical drive found");
        return fail(4, "requested drive not found");
    }
    return cmd_serve(pick);
}
