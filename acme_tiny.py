#!/usr/bin/env python3
import argparse, subprocess, json, os, sys, base64, binascii, time, hashlib, re, copy, textwrap, logging
try:
    from urllib.request import Request, urlopen, URLError, HTTPError # Python 3
    import http.client as httplib
except ImportError:
    raise ImportError('RIP Python2')

DEFAULT_CA = "https://acme-v01.api.letsencrypt.org"
USER_AGENT = 'acme-tiny // https://github.com/bjoe2k4/acme-tiny'

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.StreamHandler())
LOGGER.setLevel(logging.INFO)

def get_crt(account_key, csr, acme_dir, account_email, log=LOGGER, CA=DEFAULT_CA, chain=False):
    # helper function base64 encode for jose spec
    def _b64(b):
        return base64.urlsafe_b64encode(b).decode('utf8').replace("=", "")

    # helper function run openssl subprocess
    def _openssl(command, options, communicate=None):
        openssl = subprocess.Popen(["openssl", command] + options, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = openssl.communicate(communicate)
        if openssl.returncode != 0:
            raise IOError("OpenSSL Error: {0}".format(err))
        return out

    # setting own headers for every request
    def _request(url):
        return Request(url, headers = {'User-Agent' : USER_AGENT, 'Accept' : '*/*'})

    # parse account key to get public key
    log.info("Parsing account key...")
    pub_hex, pub_exp = re.search(
        r"modulus:\n\s+00:([a-f0-9\:\s]+?)\npublicExponent: ([0-9]+)",
        _openssl("rsa", ["-in", account_key, "-noout", "-text"]).decode('utf8'), re.MULTILINE|re.DOTALL).groups()
    pub_exp = "{0:x}".format(int(pub_exp))
    pub_exp = "0{0}".format(pub_exp) if len(pub_exp) % 2 else pub_exp
    header = {
        "alg": "RS256", "jwk": {
            "e": _b64(binascii.unhexlify(pub_exp.encode("utf-8"))), "kty": "RSA",
            "n": _b64(binascii.unhexlify(re.sub(r"(\s|:)", "", pub_hex).encode("utf-8"))),
        },
    }
    accountkey_json = json.dumps(header['jwk'], sort_keys=True, separators=(',', ':'))
    thumbprint = _b64(hashlib.sha256(accountkey_json.encode('utf8')).digest())

    # helper function make signed requests
    def _send_signed_request(url, payload, return_codes, error_message):
        payload64 = _b64(json.dumps(payload).encode('utf8'))
        protected = copy.deepcopy(header)
        protected["nonce"] = urlopen(_request(CA + "/directory")).info().get('Replay-Nonce')
        protected64 = _b64(json.dumps(protected).encode('utf8'))
        signed_request = json.dumps({
            "header": header, "protected": protected64, "payload": payload64,
            "signature": _b64(_openssl("dgst", ["-sha256", "-sign", account_key], communicate="{0}.{1}".format(protected64, payload64).encode("utf8")))
        })
        try:
            resp = urlopen(_request(url), signed_request.encode('utf8'))
            code, result, headers = resp.getcode(), resp.read(), resp.info()
        except (HTTPError, URLError) as e:
            code, result, headers = getattr(e, "code", None), getattr(e, "read", e.reason.__str__)(), getattr(e, "info", e.reason.__str__)()
        finally:
            try:
                message = return_codes[code]
                if message is not None:
                    log.info(message)
                return result, headers
            except KeyError:
                raise ValueError(error_message.format(code=code, result=result))

    # find domains
    log.info("Parsing CSR...")
    csr_dump = _openssl("req", ["-in", csr, "-noout", "-text"]).decode("utf8")
    domains = set([])
    common_name = re.search(r"Subject:.*? CN ?= ?([^\s,;/]+)", csr_dump)
    if common_name is not None:
        domains.add(common_name.group(1))
    subject_alt_names = re.search(r"X509v3 Subject Alternative Name: \n +([^\n]+)\n", csr_dump, re.MULTILINE|re.DOTALL)
    if subject_alt_names is not None:
        for san in subject_alt_names.group(1).split(", "):
            if san.startswith("DNS:"):
                domains.add(san[4:])

    # get the certificate domains and expiration
    log.info("Registering account...")

    agreement_conn = httplib.HTTPSConnection(CA.split('/')[-1])
    agreement_conn.request("HEAD", "/terms")

    payload = {
        "resource": "new-reg",
        "agreement": agreement_conn.getresponse().getheader("location"),
    }
    if account_email:
        payload["contact"] = ["mailto:{0}".format(account_email)]

    result, headers = _send_signed_request(CA + "/acme/new-reg", payload, {201: "Registered!", 409: "Already registered!"}, "Error registering: {code} {result}")

    # verify each domain
    for domain in domains:
        log.info("Verifying {0}...".format(domain))

        # get new challenge
        result, headers = _send_signed_request(CA + "/acme/new-authz", 
            {"resource": "new-authz", "identifier": {"type": "dns", "value": domain}},
            {201: None}, "Error requesting challenges: {code} {result}")

        # make the challenge file
        challenge = [c for c in json.loads(result.decode('utf8'))['challenges'] if c['type'] == "http-01"][0]
        token = re.sub(r"[^A-Za-z0-9_\-]", "_", challenge['token'])
        keyauthorization = "{0}.{1}".format(token, thumbprint)
        wellknown_path = os.path.join(acme_dir, token)
        with open(wellknown_path, "w") as wellknown_file:
            wellknown_file.write(keyauthorization)

        # check that the file is in place
        wellknown_url = "http://{0}/.well-known/acme-challenge/{1}".format(domain, token)
        try:
            resp = urlopen(_request(wellknown_url))
            resp_data = resp.read().decode('utf8').strip()
            assert resp_data == keyauthorization
        except (IOError, AssertionError):
            os.remove(wellknown_path)
            raise ValueError("Wrote file to {0}, but couldn't download {1}".format(
                wellknown_path, wellknown_url))

        # notify challenge are met
        result, headers = _send_signed_request(challenge['uri'], {"resource": "challenge","keyAuthorization": keyauthorization,},
            {202: None}, "Error triggering challenge: {code} {result}")

        # wait for challenge to be verified
        while True:
            try:
                resp = urlopen(_request(challenge['uri']))
                challenge_status = json.loads(resp.read().decode('utf8'))
            except IOError as e:
                raise ValueError("Error checking challenge: {0} {1}".format(
                    e.code, json.loads(e.read().decode('utf8'))))
            if challenge_status['status'] == "pending":
                time.sleep(2)
            elif challenge_status['status'] == "valid":
                log.info("{0} verified!".format(domain))
                os.remove(wellknown_path)
                break
            else:
                raise ValueError("{0} challenge did not pass: {1}".format(
                    domain, challenge_status))

    # get the new certificate
    log.info("Signing certificate...")
    result, headers = _send_signed_request(CA + "/acme/new-cert", {"resource": "new-cert", "csr": _b64(_openssl("req", ["-in", csr, "-outform", "DER"]))},
        {201: None}, "Error signing certificate: {code} {result}")

    certchain = [result]
    if chain:
        for header in headers.get_all("Link"):
            m = re.search(r'^\s*<([^>]*)>.*;\s*rel="up"', header)
            if m:
                log.info("Retrieving Intermediate Certificate ({0})!".format(m.group(1)))
                certchain.append(urlopen(_request(m.group(1))).read())

    # return signed certificate!
    log.info("Certificate signed!")
    return "".join(["""-----BEGIN CERTIFICATE-----\n{0}\n-----END CERTIFICATE-----\n""".format(
                    "\n".join(textwrap.wrap(base64.b64encode(cert).decode('utf8'), 64))) for cert in certchain])

def main(argv):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            This script automates the process of getting a signed TLS certificate from
            Let's Encrypt using the ACME protocol. It will need to be run on your server
            and have access to your private account key, so PLEASE READ THROUGH IT! It's
            only ~200 lines, so it won't take long.

            ===Example Usage===
            python acme_tiny.py --account-key ./account.key --csr ./domain.csr --acme-dir /usr/share/nginx/html/.well-known/acme-challenge/ > signed.crt
            ===================

            ===Example Crontab Renewal (once per month)===
            0 0 1 * * python /path/to/acme_tiny.py --account-key /path/to/account.key --csr /path/to/domain.csr --acme-dir /usr/share/nginx/html/.well-known/acme-challenge/ > /path/to/signed.crt 2>> /var/log/acme_tiny.log
            ==============================================
            """)
    )
    parser.add_argument("--account-key", required=True, help="path to your Let's Encrypt account private key")
    parser.add_argument("--csr", required=True, help="path to your certificate signing request")
    parser.add_argument("--acme-dir", required=True, help="path to the .well-known/acme-challenge/ directory")
    parser.add_argument("--account_email", help="set contact e-mail address, leave empty to keep current")
    parser.add_argument("--quiet", action="store_const", const=logging.ERROR, help="suppress output except for errors")
    parser.add_argument("--ca", default=DEFAULT_CA, help="certificate authority, default is Let's Encrypt")
    parser.add_argument("--chain", action="store_true", help="fetch and append intermediate certs to output")
    args = parser.parse_args(argv)
    LOGGER.setLevel(args.quiet or LOGGER.level)
    signed_crt = get_crt(args.account_key, args.csr, args.acme_dir, args.account_email, log=LOGGER, CA=args.ca, chain=args.chain)
    sys.stdout.write(signed_crt)

if __name__ == "__main__": # pragma: no cover
    main(sys.argv[1:])
