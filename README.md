# ArchiveBox Proxy

A proxy that saves navigated URLs on [ArchiveBox](https://github.com/mitmproxy/mitmproxy) implemented as a script to [mitmproxy](https://github.com/mitmproxy/mitmproxy).

---
This project is intended to meet ArchiveBox's ticket 557: [Feature Request: Browser extension to submit either all history or certain URLs to a given ArchiveBox instance](https://github.com/ArchiveBox/ArchiveBox/issues/577).

The main challenge is to serve ios, as ios does not allow firefox plugins to be installed.


## Installation

Pre-requisites: 

- ArchiveBox is installed and runs.
- User can run `archivebox add <url>` on the machine where this project will run, and have it add the URL to the desired instance of ArchiveBox
- python3 and pipenv are installed (else, on Debian, run `sudo apt-get install python3 pipenv` )

### Debian

1. Clone this repository and have `mitmdump` on the same path (e.g. `/home/user/archivebox-proxy` )

Please follow ( https://docs.mitmproxy.org/stable/overview-installation/ ) to install mitmdump.

```
PATH=/home/user/archivebox-proxy
git clone https://codeberg.org/brunoschroeder/archivebox-proxy.git $PATH

# mitmdump installation as of 2024-01 - alternatives exist
cd $PATH
wget https://downloads.mitmproxy.org/10.2.1/mitmproxy-10.2.1-linux-x86_64.tar.gz
tar xf mitmproxy-10.2.1-linux-x86_64.tar.gz

```

2. Edit with vim `config-archivebox-proxy.yaml` and re-define `archivebox-path`

If you wish to switch mode from **record** to **archive**, review parameter: `mode` (read section bellow about **Modes**).

3. On `$PATH`, run:

```
cd $PATH
pipenv shell
pipenv install
exit

```

4. Edit with vim `archivebox-proxy.service` and re-define `User`, `Group`, and `WorkingDirectory`

5. Run: 

```
sudo ln -sv $PATH/archivebox-proxy.service /etc/systemd/system/archivebox-proxy.servicey
sudo systemctl enable archivebox-proxy
sudo systemctl start archivebox-proxy
sudo systemctl status archivebox-proxy
q

```

6. The **archivebox-proxy** should be up and running on port 8080. Test it by configuring a browser or device:

> HTTP Proxy: <debian-server-ip>
> HTTP Port: 8080
> HTTPS Proxy: <debian-server-ip>
> HTTPS Port: 8080

HTTPS will not be working yet (bellow section on the TLS Certificate installation), test HTTP works by navigating to an HTTP only website, e.g.: `http://mitm.it`

## Configuring an HTTPS Client

One needs to install the TLS Certificate on each of the clients in order to proxy HTTPS flow.

There are several ways to do it, please refer to: ( https://docs.mitmproxy.org/stable/concepts-certificates/ )

This solution does not involve transparent proxy or services that would suffer from traffic that goes to certificate pinning endpoints.

## Modes

Config file ( `config-archivebox-proxy.yaml` ) holds a parameter for `mode` that can be: **record**, or **archive**.

The reason for two modes is explained in the section bellow **Identifying User HTTP Requests - not trivial**.

On **record mode**, archivebox-proxy will record all the navigation on `record.yaml` file, and the user will need to latter on manually run `archivebox add record.yaml`. The user may edit the file with vim and remove some of the lines ( `dd` ) with URLs not for archiving.

On **archive mode**, archivebox-proxy will run `archivebox add` to each of the identified URLs. Please read section **Identifying User HTTP requests - not trivial** bellow, before using this mode.


## Comments
### Identifying User HTTP Requests - not trivial

When developing this proxy, I came across research papers trying to solve the open problem of identifying User Actions in a HTTP Flow. It is not a trivial problem to solve as you can attest in the article bellow.

At that time (2016), some of the evidence on HTTP flows:

> "..a single request for the Huffington Post website results in the download of 408 objects from 113 unique domains. A similar analysis by Butkiewicz et al. [4], of 1,700 popular websites showed that the median landing page consists of at least 40 objects, requested from 10 or more servers, most of which are operated by third-party services."

> "..Here, the pool of starting pages is randomly selected from the top-1,000 most popular webpages according to alexa.com, excluding HTTPS pages and Chinese websites (using non-Roman script). HTTPS pages were omitted to allow fair head-to-head comparison. On average, each trace of 500 page requests resulted in 29,506 HTTP requests, distributed over 14.168 connections."

---

As of today, 2024, traffic is HTTPS, but this problem still exists. 

I implemented filters based on the authors insights and these can be tweaked by changing the float constants `__time_window_next` and `__reset_timer` in the script. (I may externalise them to the config file if users demand to constantly tinker with it).

Some more filters may be in place:

- mitmdump filter expressions, specially `'!~a'`, that filters out webpage assets. For more on these: (https://docs.mitmproxy.org/stable/concepts-filters/ )
- commercial VPNs such as ivpn and mullvad filter adverts
- projects such as [pihole](https://github.com/arkenfox/user.js)
- firefox hardening such as [arkenfox](https://github.com/arkenfox/user.js)

With all these filters running, I still get a lot of URLs that are not user action. More research must be invested on this. I count with your help on the issues forum.

---

Georgios Rizothanasis; Niklas Carlsson; Aniket Mahanti
Identifying User Actions from HTTP(S) Traffic
IEEE, 2016
( https://ieeexplore.ieee.org/document/7796839 )


### historic

2024-01 Bruno Schroeder kick-starts and asks for contribution with the architectural decisions, and delivers a script for mitmproxy.

### ios alternative solution

For each tab:

1. Hit share, and share it to iMarkdown or Obsidian 
1. Obsidian asks which file to append to - one may have one file per tag/subject
1. ios appends the url there (but sometimes it appends the page title and work must be re-done)
1. Tab must be closed



