# Security policy

The supported direct-backend version is 0.2.x. Review the limitations and evidence in
[SECURITY_REVIEW.md](SECURITY_REVIEW.md) before running against a linked account.

For sensitive findings, use GitHub's **Report a vulnerability** option if it is
available under this repository's Security tab. If it is unavailable, open a
sanitized issue requesting a private reporting channel; do not publish the exploit
or private account details there. Include the version, platform, and a minimal
reproduction using synthetic data once a private channel is established.

Never include account databases, QR pairing data, phone numbers, session keys,
captured conversations, or raw backend logs in public issues or pull requests.

This project does not promise an independent audit, safe account automation,
delivery, or exactly-once sending. Upstream Neonize/Whatsmeow/WhatsApp issues may require
separate upstream reports. No remediation response time is guaranteed.
