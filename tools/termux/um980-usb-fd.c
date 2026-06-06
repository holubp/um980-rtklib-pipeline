// Rootless Termux USB fd bridge for UM980 capture.
//
// This helper must be executed through termux-usb so Android grants an already
// authorised USB file descriptor.  It intentionally does not enumerate USB
// buses or open /dev/bus/usb paths directly.

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <libusb-1.0/libusb.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define BUF_SIZE 16384
#define MAX_COMMANDS 256
#define MAX_COMMAND_LEN 512

static volatile sig_atomic_t stop_requested = 0;

struct options {
    bool probe;
    bool read_passive;
    bool dry_run_profile;
    bool verbose;
    double duration_s;
    const char *out_path;
    const char *analysis_json;
    const char *profile_path;
    int profile_line_delay_ms;
    int capture_after_profile_delay_ms;
    int discard_after_profile_ms;
    long long discard_after_profile_bytes;
    int read_timeout_ms;
    long long max_bytes;
    int interface_override;
    int altsetting_override;
    int ep_in_override;
    int ep_out_override;
    long long expect_min_bytes;
    int profile_baud;
    int serial_baud;
    int fd;
};

struct selected_endpoint {
    int interface_number;
    int altsetting;
    unsigned char ep_in;
    unsigned char ep_out;
    unsigned char ep_in_type;
    int ep_in_max_packet_size;
    bool has_out;
};

struct command_profile {
    char commands[MAX_COMMANDS][MAX_COMMAND_LEN];
    int count;
    bool enabled;
    bool allow_reviewed_port_commands;
};

struct run_summary {
    const char *transport;
    double duration_requested_s;
    double duration_actual_s;
    long long bytes_written;
    double bytes_per_second;
    long long read_timeouts;
    long long read_errors;
    double discard_after_profile_elapsed_s;
    long long discarded_after_profile_bytes;
    long long discard_read_timeouts;
    long long discard_read_errors;
    int endpoint_in;
    int endpoint_out;
    int interface_number;
    int altsetting;
    int id_vendor;
    int id_product;
    int profile_baud;
    int serial_baud;
    bool ftdi_serial_mode;
    const char *output_path;
    int exit_status;
    const char *error_message;
};

static void on_signal(int signum) {
    (void)signum;
    stop_requested = 1;
}

static double monotonic_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static void usage(FILE *stream) {
    fprintf(stream,
            "usage: um980-usb-fd [OPTIONS] [FD]\n"
            "\n"
            "Options:\n"
            "  --probe\n"
            "  --read-passive\n"
            "  --duration SEC\n"
            "  --out FILE\n"
            "  --analysis-json FILE\n"
            "  --profile FILE\n"
            "  --dry-run-profile\n"
            "  --profile-line-delay-ms N\n"
            "  --capture-after-profile-delay-ms N\n"
            "  --discard-after-profile-ms N\n"
            "  --discard-after-profile-bytes N\n"
            "  --read-timeout-ms N\n"
            "  --max-bytes N\n"
            "  --interface N\n"
            "  --altsetting N\n"
            "  --ep-in 0xNN\n"
            "  --ep-out 0xNN\n"
            "  --expect-min-bytes N\n"
            "  --profile-baud N\n"
            "  --serial-baud N\n"
            "  --verbose\n"
            "  --help\n");
}

static bool parse_int_auto(const char *text, int *out) {
    char *end = NULL;
    errno = 0;
    long value = strtol(text, &end, 0);
    if (errno || end == text || *end != '\0' || value < 0 || value > 255) {
        return false;
    }
    *out = (int)value;
    return true;
}

static char *trim(char *s) {
    while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') {
        s++;
    }
    char *end = s + strlen(s);
    while (end > s && (end[-1] == ' ' || end[-1] == '\t' || end[-1] == '\r' || end[-1] == '\n')) {
        *--end = '\0';
    }
    return s;
}

static bool has_shell_metachar(const char *line) {
    return strpbrk(line, ";&|`$<>") != NULL;
}

static bool contains_unsafe_token(const char *line, bool allow_reviewed_port_commands, char *token_out, size_t token_out_len) {
    static const char *tokens[] = {
        "SAVECONFIG", "SAVE", "FRESET", "FACTORY", "DEFAULT", "ERASE", "FORMAT",
        "UPDATE", "UPGRADE", "BOOT", "USBMODE", "PERMANENT",
        "NVM", "FLASH", "RESET", NULL};
    static const char *reviewable_port_tokens[] = {"BAUD", "COM", NULL};
    char upper[MAX_COMMAND_LEN];
    size_t n = strlen(line);
    if (n >= sizeof(upper)) {
        n = sizeof(upper) - 1;
    }
    for (size_t i = 0; i < n; i++) {
        char c = line[i];
        upper[i] = (c >= 'a' && c <= 'z') ? (char)(c - 32) : c;
    }
    upper[n] = '\0';
    for (int i = 0; tokens[i] != NULL; i++) {
        if (strstr(upper, tokens[i]) != NULL) {
            snprintf(token_out, token_out_len, "%s", tokens[i]);
            return true;
        }
    }
    if (!allow_reviewed_port_commands) {
        for (int i = 0; reviewable_port_tokens[i] != NULL; i++) {
            if (strstr(upper, reviewable_port_tokens[i]) != NULL) {
                snprintf(token_out, token_out_len, "%s", reviewable_port_tokens[i]);
                return true;
            }
        }
    }
    return false;
}

static bool is_metadata_line(const char *line) {
    const char *colon = strchr(line, ':');
    if (colon == NULL) {
        return false;
    }
    for (const char *p = line; p < colon; p++) {
        if (*p == ' ' || *p == '\t') {
            return false;
        }
    }
    return true;
}

static int load_profile(const char *path, struct command_profile *profile, char *error, size_t error_len) {
    memset(profile, 0, sizeof(*profile));
    if (path == NULL) {
        return 0;
    }
    FILE *fp = fopen(path, "r");
    if (fp == NULL) {
        snprintf(error, error_len, "failed to open profile %s: %s", path, strerror(errno));
        return -1;
    }
    char line[MAX_COMMAND_LEN];
    int line_no = 0;
    while (fgets(line, sizeof(line), fp) != NULL) {
        line_no++;
        char *active = trim(line);
        if (*active == '\0') {
            continue;
        }
        if (*active == '#') {
            char *comment = trim(active + 1);
            if (strcasecmp(comment, "enabled: true") == 0) {
                profile->enabled = true;
            }
            continue;
        }
        if (is_metadata_line(active)) {
            if (strncasecmp(active, "enabled:", 8) == 0 &&
                (strstr(active, "true") != NULL || strstr(active, "TRUE") != NULL || strstr(active, "True") != NULL)) {
                profile->enabled = true;
            }
            if (strncasecmp(active, "allow_reviewed_port_commands:", 29) == 0 &&
                (strstr(active, "true") != NULL || strstr(active, "TRUE") != NULL || strstr(active, "True") != NULL)) {
                profile->allow_reviewed_port_commands = true;
            }
            continue;
        }
        if (has_shell_metachar(active)) {
            snprintf(error, error_len, "%s:%d unsafe shell metacharacter in active command", path, line_no);
            fclose(fp);
            return -1;
        }
        char token[64];
        if (contains_unsafe_token(active, profile->allow_reviewed_port_commands, token, sizeof(token))) {
            snprintf(error, error_len, "%s:%d unsafe receiver token %s in active command", path, line_no, token);
            fclose(fp);
            return -1;
        }
        if (profile->count >= MAX_COMMANDS) {
            snprintf(error, error_len, "%s has too many active commands", path);
            fclose(fp);
            return -1;
        }
        snprintf(profile->commands[profile->count++], MAX_COMMAND_LEN, "%s", active);
    }
    fclose(fp);
    return 0;
}

static int fd_from_env_or_args(int argc, char **argv) {
    const char *env_fd = getenv("TERMUX_USB_FD");
    if (env_fd != NULL && *env_fd != '\0') {
        return atoi(env_fd);
    }
    if (optind < argc) {
        return atoi(argv[optind]);
    }
    return -1;
}

static int parse_args(int argc, char **argv, struct options *opts) {
    memset(opts, 0, sizeof(*opts));
    opts->duration_s = 20.0;
    opts->read_timeout_ms = 1000;
    opts->profile_line_delay_ms = 100;
    opts->capture_after_profile_delay_ms = 500;
    opts->discard_after_profile_ms = 0;
    opts->discard_after_profile_bytes = 0;
    opts->max_bytes = 0;
    opts->interface_override = -1;
    opts->altsetting_override = -1;
    opts->ep_in_override = -1;
    opts->ep_out_override = -1;
    opts->profile_baud = 0;
    opts->serial_baud = 0;
    opts->fd = -1;
    static const struct option long_options[] = {
        {"probe", no_argument, 0, 1},
        {"read-passive", no_argument, 0, 2},
        {"duration", required_argument, 0, 3},
        {"out", required_argument, 0, 4},
        {"analysis-json", required_argument, 0, 5},
        {"profile", required_argument, 0, 6},
        {"dry-run-profile", no_argument, 0, 7},
        {"profile-line-delay-ms", required_argument, 0, 8},
        {"capture-after-profile-delay-ms", required_argument, 0, 9},
        {"discard-after-profile-ms", required_argument, 0, 10},
        {"discard-after-profile-bytes", required_argument, 0, 11},
        {"read-timeout-ms", required_argument, 0, 12},
        {"max-bytes", required_argument, 0, 13},
        {"interface", required_argument, 0, 14},
        {"altsetting", required_argument, 0, 15},
        {"ep-in", required_argument, 0, 16},
        {"ep-out", required_argument, 0, 17},
        {"expect-min-bytes", required_argument, 0, 18},
        {"serial-baud", required_argument, 0, 19},
        {"verbose", no_argument, 0, 20},
        {"profile-baud", required_argument, 0, 21},
        {"help", no_argument, 0, 'h'},
        {0, 0, 0, 0},
    };
    int option_index = 0;
    int c;
    while ((c = getopt_long(argc, argv, "h", long_options, &option_index)) != -1) {
        int value = 0;
        switch (c) {
        case 1: opts->probe = true; break;
        case 2: opts->read_passive = true; break;
        case 3: opts->duration_s = atof(optarg); break;
        case 4: opts->out_path = optarg; break;
        case 5: opts->analysis_json = optarg; break;
        case 6: opts->profile_path = optarg; break;
        case 7: opts->dry_run_profile = true; break;
        case 8: opts->profile_line_delay_ms = atoi(optarg); break;
        case 9: opts->capture_after_profile_delay_ms = atoi(optarg); break;
        case 10: opts->discard_after_profile_ms = atoi(optarg); break;
        case 11: opts->discard_after_profile_bytes = atoll(optarg); break;
        case 12: opts->read_timeout_ms = atoi(optarg); break;
        case 13: opts->max_bytes = atoll(optarg); break;
        case 14: opts->interface_override = atoi(optarg); break;
        case 15: opts->altsetting_override = atoi(optarg); break;
        case 16:
            if (!parse_int_auto(optarg, &value)) return -1;
            opts->ep_in_override = value;
            break;
        case 17:
            if (!parse_int_auto(optarg, &value)) return -1;
            opts->ep_out_override = value;
            break;
        case 18: opts->expect_min_bytes = atoll(optarg); break;
        case 19: opts->serial_baud = atoi(optarg); break;
        case 20: opts->verbose = true; break;
        case 21: opts->profile_baud = atoi(optarg); break;
        case 'h': usage(stdout); exit(0);
        default: usage(stderr); return -1;
        }
    }
    opts->fd = fd_from_env_or_args(argc, argv);
    return 0;
}

static const char *transfer_type_name(unsigned char attrs) {
    switch (attrs & LIBUSB_TRANSFER_TYPE_MASK) {
    case LIBUSB_TRANSFER_TYPE_BULK: return "bulk";
    case LIBUSB_TRANSFER_TYPE_INTERRUPT: return "interrupt";
    case LIBUSB_TRANSFER_TYPE_ISOCHRONOUS: return "isochronous";
    case LIBUSB_TRANSFER_TYPE_CONTROL: return "control";
    default: return "unknown";
    }
}

static void print_probe(libusb_device_handle *handle) {
    libusb_device *dev = libusb_get_device(handle);
    struct libusb_device_descriptor dd;
    if (libusb_get_device_descriptor(dev, &dd) != 0) {
        fprintf(stderr, "failed to read device descriptor\n");
        return;
    }
    printf("idVendor=0x%04x\n", dd.idVendor);
    printf("idProduct=0x%04x\n", dd.idProduct);
    printf("bDeviceClass=0x%02x\n", dd.bDeviceClass);
    printf("bDeviceSubClass=0x%02x\n", dd.bDeviceSubClass);
    printf("bDeviceProtocol=0x%02x\n", dd.bDeviceProtocol);
    printf("bNumConfigurations=%u\n", dd.bNumConfigurations);
    struct libusb_config_descriptor *config = NULL;
    if (libusb_get_active_config_descriptor(dev, &config) != 0 || config == NULL) {
        fprintf(stderr, "failed to read active config descriptor\n");
        return;
    }
    for (int i = 0; i < config->bNumInterfaces; i++) {
        const struct libusb_interface *iface = &config->interface[i];
        for (int j = 0; j < iface->num_altsetting; j++) {
            const struct libusb_interface_descriptor *alt = &iface->altsetting[j];
            printf("interface=%u altsetting=%u class=0x%02x subclass=0x%02x protocol=0x%02x\n",
                   alt->bInterfaceNumber, alt->bAlternateSetting, alt->bInterfaceClass,
                   alt->bInterfaceSubClass, alt->bInterfaceProtocol);
            for (int k = 0; k < alt->bNumEndpoints; k++) {
                const struct libusb_endpoint_descriptor *ep = &alt->endpoint[k];
                printf("  endpoint=0x%02x direction=%s transfer=%s wMaxPacketSize=%u bInterval=%u\n",
                       ep->bEndpointAddress,
                       (ep->bEndpointAddress & LIBUSB_ENDPOINT_IN) ? "IN" : "OUT",
                       transfer_type_name(ep->bmAttributes),
                       ep->wMaxPacketSize,
                       ep->bInterval);
            }
        }
    }
    libusb_free_config_descriptor(config);
}

static int select_endpoint(libusb_device_handle *handle, const struct options *opts, struct selected_endpoint *selected) {
    memset(selected, 0, sizeof(*selected));
    selected->interface_number = -1;
    selected->altsetting = -1;
    libusb_device *dev = libusb_get_device(handle);
    struct libusb_config_descriptor *config = NULL;
    if (libusb_get_active_config_descriptor(dev, &config) != 0 || config == NULL) {
        return -1;
    }
    int best_score = -100000;
    for (int i = 0; i < config->bNumInterfaces; i++) {
        const struct libusb_interface *iface = &config->interface[i];
        for (int j = 0; j < iface->num_altsetting; j++) {
            const struct libusb_interface_descriptor *alt = &iface->altsetting[j];
            if (opts->interface_override >= 0 && opts->interface_override != alt->bInterfaceNumber) continue;
            if (opts->altsetting_override >= 0 && opts->altsetting_override != alt->bAlternateSetting) continue;
            unsigned char ep_in = 0, ep_out = 0, ep_in_type = 0;
            bool has_bulk_in = false, has_bulk_out = false, has_interrupt_in = false;
            for (int k = 0; k < alt->bNumEndpoints; k++) {
                const struct libusb_endpoint_descriptor *ep = &alt->endpoint[k];
                int type = ep->bmAttributes & LIBUSB_TRANSFER_TYPE_MASK;
                bool is_in = (ep->bEndpointAddress & LIBUSB_ENDPOINT_IN) != 0;
                if (opts->ep_in_override >= 0 && ep->bEndpointAddress == opts->ep_in_override) {
                    ep_in = ep->bEndpointAddress; ep_in_type = type;
                    has_bulk_in = type == LIBUSB_TRANSFER_TYPE_BULK;
                    has_interrupt_in = type == LIBUSB_TRANSFER_TYPE_INTERRUPT;
                } else if (opts->ep_in_override < 0 && is_in && type == LIBUSB_TRANSFER_TYPE_BULK) {
                    ep_in = ep->bEndpointAddress; ep_in_type = type; has_bulk_in = true;
                } else if (opts->ep_in_override < 0 && is_in && type == LIBUSB_TRANSFER_TYPE_INTERRUPT && ep_in == 0) {
                    ep_in = ep->bEndpointAddress; ep_in_type = type; has_interrupt_in = true;
                }
                if (opts->ep_out_override >= 0 && ep->bEndpointAddress == opts->ep_out_override) {
                    ep_out = ep->bEndpointAddress; has_bulk_out = type == LIBUSB_TRANSFER_TYPE_BULK;
                } else if (opts->ep_out_override < 0 && !is_in && type == LIBUSB_TRANSFER_TYPE_BULK) {
                    ep_out = ep->bEndpointAddress; has_bulk_out = true;
                }
            }
            int score = 0;
            if (has_bulk_in) score += 100;
            else if (has_interrupt_in) score += 10;
            else score -= 1000;
            if (has_bulk_out) score += 50;
            if (alt->bInterfaceClass == 0x0a || alt->bInterfaceClass == 0xff) score += 20;
            if (score > best_score && ep_in != 0) {
                best_score = score;
                selected->interface_number = alt->bInterfaceNumber;
                selected->altsetting = alt->bAlternateSetting;
                selected->ep_in = ep_in;
                selected->ep_out = ep_out;
                selected->ep_in_type = ep_in_type;
                selected->ep_in_max_packet_size = 64;
                for (int k = 0; k < alt->bNumEndpoints; k++) {
                    const struct libusb_endpoint_descriptor *ep = &alt->endpoint[k];
                    if (ep->bEndpointAddress == ep_in) {
                        selected->ep_in_max_packet_size = ep->wMaxPacketSize > 0 ? ep->wMaxPacketSize : 64;
                    }
                }
                selected->has_out = ep_out != 0;
            }
        }
    }
    libusb_free_config_descriptor(config);
    return selected->interface_number >= 0 ? 0 : -1;
}

static int claim_selected(libusb_device_handle *handle, const struct selected_endpoint *selected) {
    int active = libusb_kernel_driver_active(handle, selected->interface_number);
    if (active == 1) {
        libusb_detach_kernel_driver(handle, selected->interface_number);
    }
    int rc = libusb_claim_interface(handle, selected->interface_number);
    if (rc != 0) {
        return rc;
    }
    libusb_set_interface_alt_setting(handle, selected->interface_number, selected->altsetting);
    return 0;
}

static int configure_ftdi_serial(libusb_device_handle *handle, int interface_number, int baud, char *error, size_t error_len) {
    if (baud <= 0) {
        return 0;
    }
    int index = interface_number + 1;
    int rc = libusb_control_transfer(handle, 0x40, 0, 0, index, NULL, 0, 1000);
    if (rc < 0) {
        snprintf(error, error_len, "FTDI reset failed: %s", libusb_error_name(rc));
        return -1;
    }
    rc = libusb_control_transfer(handle, 0x40, 0, 1, index, NULL, 0, 1000);
    if (rc < 0) {
        snprintf(error, error_len, "FTDI RX purge failed: %s", libusb_error_name(rc));
        return -1;
    }
    rc = libusb_control_transfer(handle, 0x40, 0, 2, index, NULL, 0, 1000);
    if (rc < 0) {
        snprintf(error, error_len, "FTDI TX purge failed: %s", libusb_error_name(rc));
        return -1;
    }
    rc = libusb_control_transfer(handle, 0x40, 4, 8, index, NULL, 0, 1000);
    if (rc < 0) {
        snprintf(error, error_len, "FTDI 8N1 setup failed: %s", libusb_error_name(rc));
        return -1;
    }
    static const unsigned char frac_code[8] = {0, 3, 2, 4, 1, 5, 6, 7};
    int divisor3 = (24000000 + baud / 2) / baud;
    if (divisor3 <= 0) {
        divisor3 = 1;
    }
    int divisor = (divisor3 >> 3) | (frac_code[divisor3 & 7] << 14);
    if (divisor == 1) {
        divisor = 0;
    } else if (divisor == 0x4001) {
        divisor = 1;
    }
    int value = divisor & 0xffff;
    int baud_index = index | ((divisor >> 8) & 0xff00);
    rc = libusb_control_transfer(handle, 0x40, 3, value, baud_index, NULL, 0, 1000);
    if (rc < 0) {
        snprintf(error, error_len, "FTDI baud setup failed: %s", libusb_error_name(rc));
        return -1;
    }
    return 0;
}

static size_t write_ftdi_payload(FILE *out, const unsigned char *buffer, int transferred, int max_packet_size) {
    if (max_packet_size <= 2) {
        max_packet_size = 64;
    }
    size_t written = 0;
    int offset = 0;
    while (offset < transferred) {
        int packet_len = transferred - offset;
        if (packet_len > max_packet_size) {
            packet_len = max_packet_size;
        }
        if (packet_len > 2) {
            size_t chunk = (size_t)(packet_len - 2);
            fwrite(buffer + offset + 2, 1, chunk, out);
            written += chunk;
        }
        offset += packet_len;
    }
    return written;
}

static size_t ftdi_payload_len(int transferred, int max_packet_size) {
    if (max_packet_size <= 2) {
        max_packet_size = 64;
    }
    size_t payload = 0;
    int offset = 0;
    while (offset < transferred) {
        int packet_len = transferred - offset;
        if (packet_len > max_packet_size) {
            packet_len = max_packet_size;
        }
        if (packet_len > 2) {
            payload += (size_t)(packet_len - 2);
        }
        offset += packet_len;
    }
    return payload;
}

static int send_profile(libusb_device_handle *handle, const struct selected_endpoint *selected, const struct options *opts, const struct command_profile *profile) {
    if (profile->count <= 0) {
        return 0;
    }
    if (!selected->has_out) {
        fprintf(stderr, "profile has active commands but no OUT endpoint was selected\n");
        return -1;
    }
    for (int i = 0; i < profile->count; i++) {
        char line[MAX_COMMAND_LEN + 4];
        int len = snprintf(line, sizeof(line), "%s\r\n", profile->commands[i]);
        int transferred = 0;
        int rc = libusb_bulk_transfer(handle, selected->ep_out, (unsigned char *)line, len, &transferred, 2000);
        if (rc != 0 || transferred != len) {
            fprintf(stderr, "failed to send profile line %d: rc=%d transferred=%d\n", i + 1, rc, transferred);
            return -1;
        }
        if (opts->profile_line_delay_ms > 0) {
            usleep((useconds_t)opts->profile_line_delay_ms * 1000);
        }
    }
    if (opts->capture_after_profile_delay_ms > 0) {
        usleep((useconds_t)opts->capture_after_profile_delay_ms * 1000);
    }
    return 0;
}

static int discard_after_profile_loop(libusb_device_handle *handle, const struct selected_endpoint *selected, const struct options *opts, struct run_summary *summary, bool ftdi_serial_mode) {
    if (opts->discard_after_profile_ms <= 0 && opts->discard_after_profile_bytes <= 0) {
        return 0;
    }
    unsigned char buffer[BUF_SIZE];
    double start = monotonic_s();
    double min_elapsed_s = opts->discard_after_profile_ms > 0 ? (double)opts->discard_after_profile_ms / 1000.0 : 0.0;
    long long discarded = 0;
    long long timeouts = 0;
    long long errors = 0;
    int quiet_timeouts = 0;
    while (!stop_requested) {
        double now = monotonic_s();
        bool time_met = min_elapsed_s <= 0.0 || now - start >= min_elapsed_s;
        bool bytes_met = opts->discard_after_profile_bytes <= 0 || discarded >= opts->discard_after_profile_bytes;
        if (time_met && bytes_met) {
            break;
        }
        if (min_elapsed_s <= 0.0 && opts->discard_after_profile_bytes > 0 && quiet_timeouts >= 20) {
            break;
        }
        int transferred = 0;
        int rc;
        if (selected->ep_in_type == LIBUSB_TRANSFER_TYPE_INTERRUPT) {
            rc = libusb_interrupt_transfer(handle, selected->ep_in, buffer, BUF_SIZE, &transferred, opts->read_timeout_ms);
        } else {
            rc = libusb_bulk_transfer(handle, selected->ep_in, buffer, BUF_SIZE, &transferred, opts->read_timeout_ms);
        }
        if (transferred > 0) {
            discarded += (long long)(ftdi_serial_mode ? ftdi_payload_len(transferred, selected->ep_in_max_packet_size) : (size_t)transferred);
            quiet_timeouts = 0;
        }
        if (rc == LIBUSB_ERROR_TIMEOUT) {
            timeouts++;
            quiet_timeouts++;
            continue;
        }
        if (rc == LIBUSB_ERROR_NO_DEVICE) {
            summary->error_message = "USB device disconnected during post-profile discard";
            errors++;
            break;
        }
        if (rc != 0) {
            errors++;
            if (errors > 20) {
                summary->error_message = "too many USB discard read errors";
                break;
            }
        }
    }
    summary->discard_after_profile_elapsed_s = monotonic_s() - start;
    summary->discarded_after_profile_bytes = discarded;
    summary->discard_read_timeouts = timeouts;
    summary->discard_read_errors = errors;
    return errors > 20 ? -1 : 0;
}

static int write_analysis_json(const char *path, const struct run_summary *s) {
    if (path == NULL) return 0;
    FILE *fp = fopen(path, "w");
    if (fp == NULL) return -1;
    char endpoint_out_json[16];
    if (s->endpoint_out >= 0) {
        snprintf(endpoint_out_json, sizeof(endpoint_out_json), "\"0x%02x\"", s->endpoint_out);
    } else {
        snprintf(endpoint_out_json, sizeof(endpoint_out_json), "null");
    }
    fprintf(fp,
            "{\n"
            "  \"transport\": \"termux-usb-fd\",\n"
            "  \"duration_requested_s\": %.3f,\n"
            "  \"duration_actual_s\": %.3f,\n"
            "  \"bytes_written\": %lld,\n"
            "  \"bytes_per_second\": %.3f,\n"
            "  \"read_timeouts\": %lld,\n"
            "  \"read_errors\": %lld,\n"
            "  \"discard_after_profile_elapsed_s\": %.3f,\n"
            "  \"discarded_after_profile_bytes\": %lld,\n"
            "  \"discard_read_timeouts\": %lld,\n"
            "  \"discard_read_errors\": %lld,\n"
            "  \"endpoint_in\": \"0x%02x\",\n"
            "  \"endpoint_out\": %s,\n"
            "  \"interface_number\": %d,\n"
            "  \"altsetting\": %d,\n"
            "  \"id_vendor\": \"0x%04x\",\n"
            "  \"id_product\": \"0x%04x\",\n"
            "  \"profile_baud\": %d,\n"
            "  \"serial_baud\": %d,\n"
            "  \"ftdi_serial_mode\": %s,\n"
            "  \"output_path\": \"%s\",\n"
            "  \"exit_status\": %d,\n"
            "  \"error_message\": \"%s\"\n"
            "}\n",
            s->duration_requested_s, s->duration_actual_s, s->bytes_written, s->bytes_per_second,
            s->read_timeouts, s->read_errors, s->discard_after_profile_elapsed_s,
            s->discarded_after_profile_bytes, s->discard_read_timeouts, s->discard_read_errors,
            s->endpoint_in,
            endpoint_out_json,
            s->interface_number, s->altsetting, s->id_vendor, s->id_product,
            s->profile_baud, s->serial_baud, s->ftdi_serial_mode ? "true" : "false",
            s->output_path ? s->output_path : "", s->exit_status, s->error_message ? s->error_message : "");
    fclose(fp);
    return 0;
}

static int capture_loop(libusb_device_handle *handle, const struct selected_endpoint *selected, const struct options *opts, struct run_summary *summary, bool ftdi_serial_mode) {
    FILE *out = fopen(opts->out_path, "wb");
    if (out == NULL) {
        summary->error_message = "failed to open output file";
        return -1;
    }
    unsigned char buffer[BUF_SIZE];
    double start = monotonic_s();
    long long errors = 0, timeouts = 0, bytes = 0;
    while (!stop_requested) {
        double now = monotonic_s();
        if (opts->duration_s > 0 && now - start >= opts->duration_s) break;
        if (opts->max_bytes > 0 && bytes >= opts->max_bytes) break;
        int want = BUF_SIZE;
        if (opts->max_bytes > 0 && bytes + want > opts->max_bytes) {
            want = (int)(opts->max_bytes - bytes);
        }
        int transferred = 0;
        int rc;
        if (selected->ep_in_type == LIBUSB_TRANSFER_TYPE_INTERRUPT) {
            rc = libusb_interrupt_transfer(handle, selected->ep_in, buffer, want, &transferred, opts->read_timeout_ms);
        } else {
            rc = libusb_bulk_transfer(handle, selected->ep_in, buffer, want, &transferred, opts->read_timeout_ms);
        }
        if (transferred > 0) {
            if (ftdi_serial_mode) {
                bytes += (long long)write_ftdi_payload(out, buffer, transferred, selected->ep_in_max_packet_size);
            } else {
                fwrite(buffer, 1, (size_t)transferred, out);
                bytes += transferred;
            }
        }
        if (rc == LIBUSB_ERROR_TIMEOUT) {
            timeouts++;
            continue;
        }
        if (rc == LIBUSB_ERROR_NO_DEVICE) {
            summary->error_message = "USB device disconnected";
            errors++;
            break;
        }
        if (rc != 0) {
            errors++;
            if (errors > 20) {
                summary->error_message = "too many USB read errors";
                break;
            }
        }
    }
    fclose(out);
    double elapsed = monotonic_s() - start;
    summary->duration_actual_s = elapsed;
    summary->bytes_written = bytes;
    summary->bytes_per_second = elapsed > 0 ? (double)bytes / elapsed : 0.0;
    summary->read_timeouts = timeouts;
    summary->read_errors = errors;
    if (opts->expect_min_bytes > 0 && bytes < opts->expect_min_bytes) {
        summary->error_message = "capture below expect-min-bytes";
        return -1;
    }
    return errors > 20 ? -1 : 0;
}

int main(int argc, char **argv) {
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    struct options opts;
    if (parse_args(argc, argv, &opts) != 0) {
        usage(stderr);
        return 2;
    }
    struct command_profile profile;
    char error[512] = "";
    if (load_profile(opts.profile_path, &profile, error, sizeof(error)) != 0) {
        fprintf(stderr, "%s\n", error);
        return 2;
    }
    if (opts.dry_run_profile) {
        printf("profile=%s enabled=%s commands=%d\n", opts.profile_path ? opts.profile_path : "<none>", profile.enabled ? "true" : "false", profile.count);
        for (int i = 0; i < profile.count; i++) printf("%s\n", profile.commands[i]);
        return 0;
    }
    if (opts.profile_path != NULL && !profile.enabled) {
        fprintf(stderr, "profile is disabled; refusing to send commands: %s\n", opts.profile_path);
        return 2;
    }
    if (opts.fd < 0) {
        fprintf(stderr, "no authorised USB file descriptor supplied by termux-usb\n");
        return 2;
    }
    if (!opts.probe && !opts.read_passive) {
        fprintf(stderr, "use --probe or --read-passive\n");
        return 2;
    }
    if (opts.read_passive && opts.out_path == NULL) {
        fprintf(stderr, "--out is required for --read-passive\n");
        return 2;
    }
    libusb_context *ctx = NULL;
    libusb_device_handle *handle = NULL;
    int rc = 0;
    const struct libusb_init_option init_options[] = {
        {.option = LIBUSB_OPTION_NO_DEVICE_DISCOVERY, .value = {.ival = 1}},
    };
    rc = libusb_init_context(&ctx, init_options, 1);
    if (rc != 0) {
        fprintf(stderr, "libusb_init_context(NO_DEVICE_DISCOVERY) failed: %s\n", libusb_error_name(rc));
        return 1;
    }
    rc = libusb_wrap_sys_device(ctx, (intptr_t)opts.fd, &handle);
    if (rc != 0 || handle == NULL) {
        fprintf(stderr, "libusb_wrap_sys_device failed: %s\n", libusb_error_name(rc));
        libusb_exit(ctx);
        return 1;
    }
    libusb_device *dev = libusb_get_device(handle);
    struct libusb_device_descriptor dd;
    memset(&dd, 0, sizeof(dd));
    libusb_get_device_descriptor(dev, &dd);
    if (opts.probe) {
        print_probe(handle);
    }
    struct selected_endpoint selected;
    if (select_endpoint(handle, &opts, &selected) != 0) {
        fprintf(stderr, "no usable IN endpoint found; try --interface/--altsetting/--ep-in overrides\n");
        libusb_close(handle);
        libusb_exit(ctx);
        return 1;
    }
    struct run_summary summary;
    memset(&summary, 0, sizeof(summary));
    summary.transport = "termux-usb-fd";
    summary.duration_requested_s = opts.duration_s;
    summary.endpoint_in = selected.ep_in;
    summary.endpoint_out = selected.has_out ? selected.ep_out : -1;
    summary.interface_number = selected.interface_number;
    summary.altsetting = selected.altsetting;
    summary.id_vendor = dd.idVendor;
    summary.id_product = dd.idProduct;
    summary.profile_baud = opts.profile_baud;
    summary.serial_baud = opts.serial_baud;
    summary.ftdi_serial_mode = (dd.idVendor == 0x0403 && (opts.profile_baud > 0 || opts.serial_baud > 0));
    summary.output_path = opts.out_path;
    summary.exit_status = 0;
    summary.error_message = "";
    if (opts.read_passive) {
        rc = claim_selected(handle, &selected);
        if (rc != 0) {
            fprintf(stderr, "failed to claim interface %d: %s\n", selected.interface_number, libusb_error_name(rc));
            summary.exit_status = 1;
            summary.error_message = "failed to claim interface";
        } else {
            if (opts.verbose) {
                fprintf(stderr, "selected interface=%d altsetting=%d ep_in=0x%02x ep_out=%s serial_baud=%d\n",
                        selected.interface_number, selected.altsetting, selected.ep_in,
                        selected.has_out ? "available" : "none", opts.serial_baud);
            }
            bool ftdi_serial_mode = summary.ftdi_serial_mode;
            if (ftdi_serial_mode) {
                int baud_for_profile = opts.profile_baud > 0 ? opts.profile_baud : opts.serial_baud;
                if (baud_for_profile > 0 && configure_ftdi_serial(handle, selected.interface_number, baud_for_profile, error, sizeof(error)) != 0) {
                    fprintf(stderr, "%s\n", error);
                    summary.exit_status = 1;
                    summary.error_message = "failed to configure FTDI serial bridge";
                    libusb_release_interface(handle, selected.interface_number);
                    write_analysis_json(opts.analysis_json, &summary);
                    libusb_close(handle);
                    libusb_exit(ctx);
                    return summary.exit_status;
                }
            }
            if (send_profile(handle, &selected, &opts, &profile) != 0) {
                summary.exit_status = 1;
                summary.error_message = "failed to send profile";
            } else if (ftdi_serial_mode && opts.profile_baud > 0 && opts.serial_baud > 0 && opts.profile_baud != opts.serial_baud &&
                       configure_ftdi_serial(handle, selected.interface_number, opts.serial_baud, error, sizeof(error)) != 0) {
                fprintf(stderr, "%s\n", error);
                summary.exit_status = 1;
                summary.error_message = "failed to configure FTDI serial bridge after profile";
            } else if (discard_after_profile_loop(handle, &selected, &opts, &summary, ftdi_serial_mode) != 0) {
                summary.exit_status = 1;
            } else if (capture_loop(handle, &selected, &opts, &summary, ftdi_serial_mode) != 0) {
                summary.exit_status = 1;
            }
            libusb_release_interface(handle, selected.interface_number);
        }
    }
    write_analysis_json(opts.analysis_json, &summary);
    libusb_close(handle);
    libusb_exit(ctx);
    return summary.exit_status;
}
