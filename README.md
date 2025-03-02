# media-vault
Tool for backing up media files

## Usage

### Install

```bash
hatch shell
hatch build && pip install . && media-vault hello
```

### External Drive
```bash
sudo mount -t ntfs-3g /dev/sdc1 /mnt
```

## Development

```bash
hatch run types:check
hatch fmt
```
