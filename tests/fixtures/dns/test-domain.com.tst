; This file is used in unit tests for OctoDNS
$ORIGIN test-domain.com.
@           3600 IN	SOA	ns1.test-domain.com. root.test-domain.com. (
                        2018071501		; Serial
                        3600    ; Refresh (1 hour)
                        600     ; Retry (10 minutes)
                        604800  ; Expire (1 week)
                        3600    ; NXDOMAIN ttl (1 hour)
                    )

; NS Records
@           3600  IN  NS  ns1.test-domain.com.
@           3600  IN  NS  ns2.test-domain.com.
under       3600  IN  NS  ns1.test-domain.com.
under       3600  IN  NS  ns2.test-domain.com.

; CAA Records
caa         1800  IN  CAA 0 issue "ca.test-domain.com"
caa         1800  IN  CAA 0 iodef "mailto:admin@test-domain.com"

; SRV Records
_srv._tcp   600   IN  SRV 10 20 30 foo-1.test-domain.com.
_srv._tcp   600   IN  SRV 10 20 30 foo-2.test-domain.com.
; NULL SRV Records
_pop3._tcp   600   IN  SRV 0 0 0 .
_imap._tcp   600   IN  SRV 0 0 0 .

; TXT Records
txt         600   IN  TXT "Bah bah black sheep"
txt         600   IN  TXT "have you any wool."
txt         600   IN  TXT "v=DKIM1;k=rsa;s=email;h=sha256;p=A/kinda+of/long/string+with+numb3rs"

; MX Records
mx          300   IN  MX  10  smtp-4.test-domain.com.
mx          300   IN  MX  20  smtp-2.test-domain.com.
mx          300   IN  MX  30  smtp-3.test-domain.com.
mx          300   IN  MX  40  smtp-1.test-domain.com.

; LOC Records
loc         300   IN  LOC 31 58 52.1 S 115 49 11.7 E 20m 10m 10m 2m
loc         300   IN  LOC 53 14 10 N 2 18 26 W 20m 10m 1000m 2m

; A Records
@           300   IN  A   1.2.3.4
@           300   IN  A   1.2.3.5
www         300   IN  A   2.2.3.6
wwww.sub    300   IN  A   2.2.3.6

; AAAA Records
aaaa        600   IN  AAAA  2601:644:500:e210:62f8:1dff:feb8:947a

; CNAME Records
cname       300   IN  CNAME   test-domain.com.
included    300   IN  CNAME   test-domain.com.
