#pragma once

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdint>
#include <cstdlib>
#include <cerrno>
#include <iostream>

#include "core/communication/ring.h"

int socket_create(int port) {
    int server_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (server_sock == -1) {
        perror("socket failed");
        throw std::runtime_error("Socket failed");
        return -1;
    }

    int opt = 1;
    if (setsockopt(server_sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        perror("setsockopt failed");
        close(server_sock);
        throw std::runtime_error("setsockopt failed");
    }

    struct sockaddr_in address;
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port = htons(port);

    // Attempt to bind; on failure, try again after 1 minute
    // This may help recover from intermittent socket issues.
    if (bind(server_sock, (struct sockaddr*)&address, sizeof(address)) == -1) {
        auto m = "bind failed on port " + std::to_string(port) + ": trying again after 60 sec";
        perror(m.c_str());
        sleep(60);
        if (bind(server_sock, (struct sockaddr*)&address, sizeof(address)) == -1) {
            close(server_sock);
            throw std::runtime_error("Bind failed twice, exiting!");
            return -1;
        }
        std::cout << "...OK!\n";
    }

    socklen_t addrlen = sizeof(address);
    if (getsockname(server_sock, (struct sockaddr*)&address, &addrlen) == -1) {
        perror("getsockname failed");
        close(server_sock);
        throw std::runtime_error("getsockname failed");
        return -1;
    }

    char ipstr[INET_ADDRSTRLEN] = {0};
    if (inet_ntop(AF_INET, &address.sin_addr, ipstr, sizeof(ipstr)) == NULL) {
        perror("inet_ntop failed");
        close(server_sock);
        throw std::runtime_error("inet_ntop failed");
        return -1;
    }

    return server_sock;
}

int socket_connect_direct(const std::string& hostname, int port) {
    struct addrinfo hints{}, *res;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;

    std::string port_str = std::to_string(port);
    int status = getaddrinfo(hostname.c_str(), port_str.c_str(), &hints, &res);
    if (status != 0) throw std::runtime_error("Failed to resolve hostname: " + hostname);

    int sockfd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (sockfd < 0) {
        freeaddrinfo(res);
        throw std::runtime_error("Failed to create socket");
    }

    int opt = 1;
    if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
        close(sockfd);
        throw std::runtime_error("Could not set SO_REUSEADDR");
    }

    const int max_retries = 3;
    for (int attempts = 0; connect(sockfd, res->ai_addr, res->ai_addrlen) < 0; ++attempts) {
        if (attempts >= max_retries) {
            close(sockfd);
            freeaddrinfo(res);
            throw std::runtime_error("Failed to connect to server (" + hostname + ":" +
                                     std::to_string(port) + ") after " +
                                     std::to_string(max_retries) + " attempts");
        }
        sleep(1);
    }

    freeaddrinfo(res);
    return sockfd;
}

// Opt-in userspace relay. Ordinary NoCopy connections are unchanged when unset.
int socket_connect(const std::string& hostname, int port) {
    const char* relay_port = std::getenv("ORQ_WAN_PROXY_PORT");
    if (!relay_port) return socket_connect_direct(hostname, port);
    char* end = nullptr;
    const long parsed = std::strtol(relay_port, &end, 10);
    if (!*relay_port || *end || parsed < 1 || parsed > 65535 ||
        hostname.find_first_of("\t\r\n") != std::string::npos) {
        throw std::runtime_error("Invalid userspace WAN relay endpoint");
    }
    int sock = socket_connect_direct("127.0.0.1", static_cast<int>(parsed));
    const std::string request = hostname + "\t" + std::to_string(port) + "\n";
    size_t sent = 0;
    while (sent < request.size()) {
        auto n = send(sock, request.data() + sent, request.size() - sent, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) {
            close(sock);
            throw std::runtime_error("Userspace WAN relay handshake send failed");
        }
        sent += n;
    }
    char reply = 0;
    ssize_t received;
    do { received = recv(sock, &reply, 1, 0); } while (received < 0 && errno == EINTR);
    if (received != 1 || reply != 'O') {
        close(sock);
        throw std::runtime_error("Userspace WAN relay connection failed");
    }
    return sock;
}

int send_meta(int sockfd, int byte_count) {
    int ret = send(sockfd, &byte_count, sizeof(byte_count), 0);
    if (ret == -1) {
        throw std::runtime_error("send_: Send failed");
    }
    return 1;
}

int recv_meta(int sockfd) {
    int byte_count;
    int ret = recv(sockfd, &byte_count, sizeof(byte_count), 0);

    if (ret == -1) {
        throw std::runtime_error("recv_meta failed");
    } else if (ret == 0) {
        // Break connection
        return -1;
    }
    return byte_count;
}

int send_message(int sockfd, const RingEntry& entry) {
    int result = send(sockfd, entry.buffer, entry.used, 0);

    if (result == -1) {
        throw std::runtime_error("send_message: Send failed");
    }

    return 1;
}

size_t send_wrapper(int sockfd, const char* buf, ssize_t buf_size) {
    ssize_t bytes_sent = 0;

    while (bytes_sent < buf_size) {
        ssize_t ret = send(sockfd, buf + bytes_sent, buf_size - bytes_sent, 0);

        if (ret < 0) {
            throw std::runtime_error("send_wrapper: Send failed");
        }

        bytes_sent += ret;

        // std::cout << "send loop: " << bytes_sent << "/" << buf_size << "\n";
    }

    return bytes_sent;
}

int recv_message(int sockfd, char* buf, ssize_t buf_size) {
    ssize_t bytes_received = 0;

    while (bytes_received < buf_size) {
        ssize_t ret = recv(sockfd, buf + bytes_received, buf_size - bytes_received, 0);

        if (ret == -1) {
            throw std::runtime_error("Recv failed");
        } else if (ret == 0) {
            // Break connection
            return -1;
        }
        bytes_received += ret;
    }

    return bytes_received;
}
