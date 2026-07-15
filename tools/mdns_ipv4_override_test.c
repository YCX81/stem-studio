#include <stdio.h>
#include <stdlib.h>

#ifdef WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#endif

#include "mdnsd.h"

int main(void)
{
    struct in_addr expected;
    uint32_t actual;

#ifdef WIN32
    _putenv_s("STEM_STUDIO_MDNS_IPV4", "192.168.31.79");
#else
    setenv("STEM_STUDIO_MDNS_IPV4", "192.168.31.79", 1);
#endif

    if (inet_pton(AF_INET, "192.168.31.79", &expected) != 1) {
        fprintf(stderr, "failed to prepare expected IPv4 address\n");
        return 1;
    }

    actual = mdnsd_select_ipv4();
    if (actual != expected.s_addr) {
        fprintf(stderr, "mDNS override was ignored\n");
        return 1;
    }

    return 0;
}
