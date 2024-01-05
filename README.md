
ArchiveBox Proxy

A proxy for ArchiveBox with two modes: archive-all or archive-the-list.

On archive-the-list, the proxy is configured with a regex list of what to archive.

On archive-all, the proxy is configured with a regex list of what NOT to archive.

The config list will carry for each regex:
- tags to be applied
- how often that link should be archived

---
The proxy will also provide a url to be used as prefix to an open URL in the browser or terminal, to submit that URL to be archived regardless of what's on the list.

---
This project is intended to meet ArchiveBox's ticket 557: [Feature Request: Browser extension to submit either all history or certain URLs to a given ArchiveBox instance](https://github.com/ArchiveBox/ArchiveBox/issues/577).

The main challenge is to serve ios, as ios does not allow firefox plugins to be installed.

### historic

2024-01 Bruno Schroeder kick-starts and asks for contribution with the architectural decisions. 

### ios alternative solution

For each tab:

1. Hit share, and share it to iMarkdown or Obsidian 
1. Obsidian asks which file to append to - one may have one file per tag/subject
1. ios appends the url there (but sometimes it appends the page title and work must be re-done)
1. Tab must be closed





