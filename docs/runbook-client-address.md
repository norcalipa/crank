<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->

# Operator Runbook: Trusted Client Address

**Owner:** CRank maintainers
**Last reviewed:** 2026-10-10
**Version/change process:** Changes follow the [architecture documentation](./readme.md) and must pass CI gates.

## Purpose

Decide whether the app may trust a client network address, turn that on, check
it, and turn it off again. The address is used for one thing: per-address
limits on company corrections, so that one person cannot multiply the
per-account limits by creating accounts (issue #538).

**The switch is off by default and this runbook is the only way to turn it
on.** While it is off, production keeps the per-account limits only, exactly
as before.

Never write an address, a hostname or a tunnel id into this repository, a pull
request or an issue. The commands below use placeholders; keep the real values
out of anything you paste.

## Why a gate is needed

Requests reach the app like this:

```text
Cloudflare edge -> Cloudflare Tunnel -> cloudflared on the node
    -> NodePort 30800 on loopback -> kube-proxy -> pod :8080
```

- The tunnel connects outward, so no inbound port is needed.
- The peer the app sees (`REMOTE_ADDR`) is a node address for every request,
  whether it came through the tunnel or straight to the NodePort. **The app
  cannot tell the two apart.**
- So the app can trust a header from Cloudflare only if Cloudflare is the only
  way in. That is a fact about the network, and only the operator can check it.

### Which proxies may set which header

| Value | Who sets it | Trusted by the app |
|---|---|---|
| `CF-Connecting-IP` | Cloudflare's edge for the `crank.fyi` zone, on every proxied request, always as one address. Cloudflare documents a request that arrives with its own `CF-Connecting-IP` as a cause of Error 1000 (checked in V3). cloudflared and kube-proxy pass the header through. | Only with `ENV=prod` and `CLIENT_IP_SOURCE=cloudflare`, and only by `crank/client_address.py`. |
| `X-Forwarded-Proto` | Cloudflare's edge / cloudflared. | Already trusted in production (`SECURE_PROXY_SSL_HEADER`). It depends on the same lockdown. |
| `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP`, `Forwarded` | Anyone. Cloudflare appends to what the client sent. | Never. |
| `REMOTE_ADDR` | The TCP peer, which is a node address for every request. | Never as a client address. |

**Anyone who can open a connection to the pod can send `CF-Connecting-IP`.**
The table says who is supposed to; the gate below is what makes it true.

## What the switch does

- `CLIENT_IP_SOURCE` names a source, not a header. The only source is
  `cloudflare`, which reads `CF-Connecting-IP`. Any other value is ignored
  (system check `crank.W003`), and any value outside `ENV=prod` is ignored
  (`crank.W004`). Neither stops the app from starting.
- With the source on and an address identified, each IPv4 address or IPv6 /64
  may store at most `COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR` (20)
  corrections and make at most `COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR`
  (120) rejected attempts an hour, across all accounts. The per-account limits
  (10 and 60) are checked first and are not loosened.
- A request whose header is missing, duplicated or malformed is not refused and
  is not put in a shared bucket: it gets the per-account limits only, and the
  app logs `client address unavailable` at most once every 10 minutes.
- The limiter keeps a keyed digest of the address in the cache for up to one
  hour. No address is written to the database, logs or telemetry.

## The gate

Every step here needs access the software factory does not have: the router,
root on the node, the cluster and the Cloudflare dashboard.

### P0. Record the path

On the node:

```sh
sudo cat /etc/cloudflared/config.yml                      # expect only the site hostnames -> http://localhost:30800
k3s kubectl get nodes -o wide                             # node count
k3s kubectl -n crank get svc crank -o wide                # NodePort 8080:30800
k3s kubectl get svc -A | grep -E 'NodePort|LoadBalancer'  # other services a NodePort bind change affects
```

### G1. The public internet cannot reach the app except through Cloudflare

Required.

1. **Router.** No port-forward, DMZ host or UPnP/NAT-PMP mapping targets the
   node on tcp/30800. If the router forwards any other port to the node, note
   which, for step 2.
2. **From outside the home network** (a phone on mobile data with Wi-Fi off, or
   any external host), using the WAN address shown on the router's status page:

   ```sh
   curl -m 8 -sS -o /dev/null -w '%{http_code}\n' http://<WAN-ADDRESS>:30800/healthz/ready/
   # pass: timeout or connection refused (curl exit 28 or 7)
   for u in http://<WAN-ADDRESS>:80 http://<WAN-ADDRESS>:8080 https://<WAN-ADDRESS>:443 https://<WAN-ADDRESS>:8443; do
     curl -m 8 -sk -o /dev/null -w "$u %{http_code}\n" -H 'Host: crank.fyi' "$u/healthz/ready/"
   done
   # pass: none returns 200 with the readiness JSON (add any other port the router forwards)
   ```

3. **IPv6.** `ip -6 addr show scope global` on the node shows no address
   outside `fc00::/7`, or the router drops unsolicited inbound IPv6. If the
   node has a global address, repeat step 2 against `[address]:30800`.
4. **No other way in.** `tailscale funnel status` and `tailscale serve status`
   expose nothing that reaches the app; no other reverse tunnel runs on the
   node; the Cloudflare Zero Trust dashboard shows one tunnel with the
   hostnames from P0.
5. **Edge settings that change the header.** In the `crank.fyi` zone: Pseudo
   IPv4 is Off or "Add header" (not "Overwrite headers"); the managed transform
   "Remove visitor IP headers" is off; no Worker route rewrites requests to the
   origin.

**If any of 1–4 fails:** remove the forward or tunnel and repeat. If it cannot
be removed, stop. Do not enable.

**Restricting a forwarded port to Cloudflare's address ranges is not an
accepted remedy.** Cloudflare documents that a Worker which fetches a
non-Cloudflare origin directly controls `CF-Connecting-IP`, so any Cloudflare
customer could reach such a port from Cloudflare's addresses with a forged
value. The tunnel needs no open port, so the remedy is to close it.

### G2. LAN, tailnet and VPN devices cannot reach the app directly

Required, unless the owner waives it in writing (record `waived`).

Check, from another device on the LAN and from another device on the tailnet:

```sh
curl -m 5 -sS -o /dev/null -w '%{http_code}\n' http://<NODE-ADDRESS>:30800/healthz/ready/   # pass: timeout or refused
```

Lock down with one of these. None has been tested by the factory; each has its
own check and rollback.

- **Option A: bind NodePorts to loopback (recommended; smallest change).** Add
  to `/etc/rancher/k3s/config.yaml`, then `sudo systemctl restart k3s`:

  ```yaml
  kube-proxy-arg:
    - "nodeport-addresses=127.0.0.0/8"
  ```

  - Check `systemctl cat k3s` first: if the unit already passes
    `--kube-proxy-arg`, add the value there instead.
  - This applies to **every** NodePort service on the cluster. Review the P0
    list first.
  - Check afterwards: `curl http://127.0.0.1:30800/healthz/ready/` on the node
    returns 200, the same request to the node's LAN address fails, and
    `https://crank.fyi/` still loads.
  - Loopback NodePorts depend on kube-proxy's mode (its nftables mode does not
    serve them). If the loopback request fails after the restart, remove the
    line, restart again and use Option B.
  - **This setting lives on the host, not in this repository.** A rebuild or a
    k3s reinstall silently drops it; repeat the gate after either.
- **Option B: no NodePort at all (versioned in the repository).**
  1. Read the Service address:
     `k3s kubectl -n crank get svc crank -o jsonpath='{.spec.clusterIP}'`.
  2. Point both hostnames in `/etc/cloudflared/config.yml` at
     `http://<that address>:8080`, `sudo systemctl restart cloudflared`, and
     check the site.
  3. Merge a manifest PR that makes the Service `type: ClusterIP`, removes
     `nodePort: 30800` and pins `clusterIP:` to the same address. Check the
     site again, and that `:30800` no longer answers anywhere.
  - Do the steps in this order; reversing 2 and 3 takes the site down.
  - A LAN device that adds its own route to the cluster's service range through
    the node could still reach it. That needs deliberate configuration on that
    device.
- **Option C: host firewall.** Drop traffic to tcp/30800 that does not arrive
  on `lo`, in a chain that runs before kube-proxy's DNAT, and make it
  persistent. **`ufw deny 30800` does not work**: NodePort traffic is rewritten
  in PREROUTING and passes through FORWARD, never INPUT.

Whatever is chosen, these can still reach the pod and are accepted as inside
the trust boundary: processes on the node itself, and other pods in the
cluster.

## Record, then enable

1. Add one record to `docs/client-address-verification.yaml`. It holds dates
   and pass/fail values only: **no addresses, no hostnames, no tunnel id.**

   ```yaml
   verifications:
     - date: "2026-10-12"
       verified_by: "Operator Name"
       g1_public_path: pass          # pass | fail
       g2_private_path: pass         # pass | waived | fail
       g2_method: nodeport-addresses # nodeport-addresses | clusterip | firewall | waived
       edge_header_checks: pass      # G1 step 5: pass | fail
   ```

2. In the same pull request, add `CLIENT_IP_SOURCE: "cloudflare"` to
   `k8s/crank-configmap.yml`.
3. CI enforces the coupling (`crank/tests/test_client_address.py`): with
   `cloudflare` in the manifest, the last record must have
   `g1_public_path: pass`, `g2_private_path` of `pass` or `waived`,
   `edge_header_checks: pass`, a name, and a date that is not in the future.
   The record is the operator's attestation; CI cannot check that it is true.
4. Merge only after the "Deploy to Kubernetes (prod)" run of the release that
   contains the limiter has finished. The manifest-only workflow deploys image
   tag `latest`, and that run is what moves `latest`.

If the gate cannot be confirmed, do not open that pull request. The
per-address layer stays off and production keeps the per-account limits.

## Verify in production after enabling

- **V1.** `k3s kubectl -n crank exec deploy/crank -- printenv CLIENT_IP_SOURCE`
  prints `cloudflare`. If it is empty the pods did not restart (expected when
  the Deployment was already on `latest`): run
  `k3s kubectl -n crank rollout restart deployment/crank`.
- **V2.** Signed in as staff at `https://crank.fyi/staff/release-diagnostics/`:
  "Client address source" says `cloudflare`, and "Client address for this
  request" matches your public address (for IPv6, its /64).
- **V3.** In the browser console on that page:

  ```js
  fetch('/staff/release-diagnostics/', {headers: {'CF-Connecting-IP': '203.0.113.9'}})
    .then(async r => console.log(r.status, (await r.text()).includes('203.0.113.9')))
  ```

  Pass: the second value is `false`. The expected result is Cloudflare's Error
  1000 page; the page still showing your real address also passes. If it is
  `true`, **roll back at once**: the edge is passing a client-supplied value.
- **V4.** The same page from a second network (a phone on mobile data) shows a
  different address.
- **V5.** Submit one correction through the site, then reject it in admin. It
  returns 201, and the pod logs have no `client address unavailable` warning.

## Roll back

Revert the enablement pull request. The value returns to empty, the layer is
off, and the counters expire within the hour. Check with V1.

Do not use `kubectl set env`: it adds a container-level variable that later
deploys do not remove.

## When to repeat the gate

Repeat G1 and G2 after any of:

- a new or reconfigured router;
- an ISP change, or IPv6 being enabled;
- a new tunnel, Funnel, VPN or port-forward;
- a k3s reinstall or upgrade that could reset kube-proxy arguments;
- a second node;
- a change to cloudflared's ingress;
- a change to the zone's network or transform settings;
- replacing `runserver` with another server. The app relies on the server
  dropping header names that contain underscores, so that `CF_Connecting_IP`
  cannot stand in for `CF-Connecting-IP`. Django's WSGI request handler does
  this; its ASGI handler does not.

Nothing detects a new path automatically. If one appears while the switch is
on, anyone on it can forge the header: they avoid the per-address layer (the
per-account limits still hold) and can use up a chosen address's allowance for
up to an hour. No data is exposed.

## Related

- [Threat model: abuse limits and client address](./threat-model-agentic-features.md)
- [Capability configuration contract: staff diagnostics](./capability-config-contract.md)
