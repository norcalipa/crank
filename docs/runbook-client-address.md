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
  `cloudflare`, which reads `CF-Connecting-IP`. In production any other value
  is ignored (system check `crank.W003`). Outside `ENV=prod` the variable is
  not read at all; `python manage.py check` prints `crank.W004` when it finds
  the variable set in such an environment. Neither stops the app from
  starting.
- With the source on and an address identified, each IPv4 address or IPv6 /64
  may store at most `COMPANY_CORRECTION_IP_RATE_LIMIT_PER_HOUR` (20)
  corrections and make at most `COMPANY_CORRECTION_IP_REJECTED_LIMIT_PER_HOUR`
  (120) rejected attempts an hour, across all accounts. The per-account limits
  (10 and 60) are checked first and are not loosened.
- A request whose header is missing, duplicated or malformed is not refused and
  is not put in a shared bucket: it gets the per-account limits only, and the
  app logs `client address unavailable` at most once every 10 minutes, with
  the reason (`missing`, `multiple`, `invalid` or `special`) and never the
  value.
- The same applies to an address no visitor can have (reason `special`):
  unspecified (`0.0.0.0/8`, `::`), loopback (`127.0.0.0/8`, `::1`), private
  (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), shared
  (`100.64.0.0/10`), link-local (`169.254.0.0/16`, `fe80::/10`), multicast
  (`224.0.0.0/4`, `ff00::/8`), reserved (`240.0.0.0/4`) and all of `::/8`,
  including the mapped and embedded IPv4 forms of the above. Cloudflare never
  reports one of these for a visitor, so seeing `special` means a request
  reached the pod by another path, or Pseudo IPv4 is set to "Overwrite
  headers" (G1 step 5). It is not a defence against forging: a forger would
  send a public address.
- The hour for stored corrections starts at the first request of the hour. The
  hour for rejected attempts starts at the first rejected attempt. Both are
  unchanged from before #538 for the per-account limits, whether the switch
  is on or off.
- A request holds its place against each limit until it finishes, so limits
  also cap requests in flight. A request refused only because others from the
  same account or address were still in flight gets a `429`. Against the
  rejected-attempt limits that refusal carries `Retry-After: 1`. Against the
  stored limits (10 per account as before, 20 per address) it carries the
  rest of the hour, as it did before #538, even if none of the requests in
  flight ends up stored; the app cannot tell the two cases apart there. It
  takes more than 20 simultaneous submissions from one address to see this.
- The in-flight count behind the rejected-attempt limits cleans up after
  itself. If a request never finishes (the cache fails mid-request, or a
  worker is stopped), its place stops counting within two minutes, however
  busy the account or address is. A request that runs longer than a minute
  may stop being counted as in flight before it ends.
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

   `data` of the ConfigMap `crank-config` is the only place the switch may be
   set. The same test walks every file git tracks, wherever it lives, and
   fails, whatever the record says, if:

   - a YAML or JSON manifest anywhere in the repository sets
     `CLIENT_IP_SOURCE` in another ConfigMap, a second `crank-config`, a
     container `env:` entry or a Secret, including inside a `kind: List`;
   - any other tracked file names `CLIENT_IP_SOURCE` outside a comment: a
     workflow, a composite action, a script, a Dockerfile,
     `docker-compose.yml` or application code. Only the code that reads the
     switch (`crank/settings/base.py`, `crank/settings/prod.py`,
     `crank/checks.py`, `crank/client_address.py`, `crank/release.py`) and
     the Python, Markdown and `.yaml` files under `crank/tests/` and `docs/`
     may name it;
   - a workflow creates a Secret that carries it, or creates one from a file;
   - a container loads a ConfigMap or Secret the test cannot account for;
   - a workflow or script hands a secrets-named file to `kubectl apply`, on
     one line or continued over several;
   - a file named `.env` is tracked anywhere, any `.env*` file other than
     `.env-prod` is tracked, or `.dockerignore` no longer keeps `.env` out of
     the image. The app calls `load_dotenv()`, so a `.env` inside the image
     would set the variable.

   The enabling pull request therefore changes two files, the ConfigMap and
   the record, and no test.

   What CI cannot see. The list below is what is known today, not a proof
   that nothing else passes; closing the routes marked "open" is tracked in
   <https://github.com/norcalipa/crank/issues/566>:

   - anything applied to the cluster by hand: `kubectl edit`,
     `kubectl set env`, `kubectl apply` of a file that is not in the
     repository, a key added to a ConfigMap or Secret;
   - the contents of Secrets the operator creates in the cluster
     (`db-connect-credentials`, accepted by name);
   - the contents of secrets-named files and of `.env-prod`: the test never
     opens them. The deploy workflows copy `k8s/crank-capability-secrets.yml`
     to the node and do not apply it. `.env-prod` is a file inside the image
     (`ADD . /app`, and `.dockerignore` excludes only `.env` and `**/.env`),
     and the image build job reads it: it runs `source .env-prod` and passes
     its lines to the build as build arguments, and neither Dockerfile
     declares an `ARG`. Nothing loads it at run time today, which the tests
     pin: every `load_dotenv()` call takes no argument, so only a file named
     `.env` is looked for, and no Dockerfile or manifest names `.env-prod`.
     A change that made the build copy it to `.env`, loaded it by name, or
     had a container command source it would not be seen by name alone
     (open);
   - a secrets-named file copied to another name and applied under that name;
   - a name put together at run time instead of written out: by the shell,
     by `envsubst`, by a YAML escape inside a command, or by a Dockerfile
     `ENV` whose name is a build argument;
   - a command written inside a test or a document that may name the
     switch, if a workflow were changed to run that file: those files are
     not read as commands;
   - files that are not UTF-8 text, and files git does not track;
   - environment set outside the repository at run time: on the node, in a
     `.env` file written into a running container or a mounted volume, or in
     an image built from something other than this repository;
   - a second document in `k8s/crank-configmap.yml` that is not a ConfigMap
     and names the switch in a container `command:`: that one file is
     excluded from the text scan, and both deploy workflows apply it as it
     is (open);
   - a secrets-named file handed to `kubectl create -f` or
     `kubectl replace -f` (the check looks for `apply`), or picked up by
     `kubectl apply -f` on a directory (open);
   - whether the record is true.

   V1 below reads the environment of one running pod, so it sees what the
   pod's spec and the image put in the environment of a new process. It
   cannot see a value that `load_dotenv()` reads from a `.env` file inside
   the running container or a mounted volume: that value exists only inside
   the Python process, and `printenv` in a fresh process prints nothing for
   it. The check for that route is V2 below: before enabling, "Client
   address source" must read `none`, the value the serving process has
   (the pod that answered the request, so repeat it if there are several).
   Run both before opening the enabling pull request.
4. Merge only after the "Deploy to Kubernetes (prod)" run of the release that
   contains the limiter has finished. The manifest-only workflow deploys image
   tag `latest`, and that run is what moves `latest`.

If the gate cannot be confirmed, do not open that pull request. The
per-address layer stays off and production keeps the per-account limits.

## Verify in production after enabling

- **V1.** `k3s kubectl -n crank exec deploy/crank -- printenv CLIENT_IP_SOURCE`
  prints `cloudflare`. If it is empty the pods did not restart (expected when
  the Deployment was already on `latest`): run
  `k3s kubectl -n crank rollout restart deployment/crank`. Before enabling,
  the same command must print nothing: a value then was set in the cluster by
  hand, outside what CI checks, and must be removed first.
- **V2.** Signed in as staff at `https://crank.fyi/staff/release-diagnostics/`:
  "Client address source" says `cloudflare`, and "Client address for this
  request" matches your public address (for IPv6, its /64). While the switch
  is off the two rows read `none` and `off (no address is read)`; `on, not
  identified` means the switch is on and this request carried no usable
  address, which is a failure here.
- **V3.** In the browser console on that page, send your own
  `CF-Connecting-IP` and read the row that comes back:

  ```js
  fetch('/staff/release-diagnostics/', {headers: {'CF-Connecting-IP': '203.0.113.9'}})
    .then(async r => {
      const page = new DOMParser().parseFromString(await r.text(), 'text/html');
      const row = [...page.querySelectorAll('th')]
        .find(th => th.textContent === 'Client address for this request');
      console.log(r.status, row ? row.nextElementSibling.textContent : 'no such row');
    });
  ```

  There are three possible results. "The forged value is not on the page" is
  not enough to pass: the third result does not show it either.

  | What the edge did | What the script prints | Result |
  |---|---|---|
  | Rejected the request | `403 no such row` (Cloudflare's Error 1000 page). This is the expected result. | **Pass** |
  | Replaced the header | `200` and the same address as in V2. | **Pass** |
  | Forwarded the header | `200 203.0.113.9`, or `200 on, not identified` (the app received your value beside the edge's and refused both). | **Fail: roll back at once** |

  Either failing result means any client can send the header and so choose its
  bucket or drop out of the per-address layer. Any other output (another
  status, another address) is also a failure until explained.
- **V4.** The same page from a second network (a phone on mobile data) shows a
  different address.
- **V5.** Submit one correction through the site, then reject it in admin. It
  returns 201, and the logs of **every** pod
  (`k3s kubectl -n crank logs -l app.kubernetes.io/name=crank --since=15m`)
  have no `client address unavailable` warning. The warning is written by one
  pod, at most once every 10 minutes, so one pod's logs are not enough.

## Roll back

1. Revert the enablement pull request and let its deploy finish.
2. Restart the pods now, without waiting to see whether the deploy did:
   `k3s kubectl -n crank rollout restart deployment/crank`, then
   `k3s kubectl -n crank rollout status deployment/crank`. If the Deployment
   was already on `latest` the revert changes no pod, and the pods keep the
   switch until something restarts them.
3. A rollback has passed when both of these hold:
   - `printenv CLIENT_IP_SOURCE` prints **nothing** in **every** pod:

     ```sh
     for p in $(k3s kubectl -n crank get pods -l app.kubernetes.io/name=crank -o name); do
       k3s kubectl -n crank exec "$p" -- printenv CLIENT_IP_SOURCE || echo "$p: empty"
     done
     ```

     Every line must end in `empty`. (This is V1 with the opposite expected
     result: there `cloudflare` is the pass, here it is the failure.)
   - the staff page shows "Client address source" `none` and "Client address
     for this request" `off (no address is read)`.

The layer is then off, and the per-address counters expire within the hour.

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
