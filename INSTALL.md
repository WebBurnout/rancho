
Flash a new Raspberry Pi OS image. Make sure it's the "lite" version with no GUI. Add the user and ssh keys per wizard instructions.

It will take a long time to boot the first time. Once you're in, setup passwordless sudo:

```
echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010-$USER-nopasswd
sudo chmod 440 /etc/sudoers.d/010-$USER-nopasswd
sudo visudo -c
```

Turn off password auth in ssh:

```
echo "PasswordAuthentication no" | sudo tee /etc/ssh/sshd_config.d/no-passwords.conf
sudo systemctl restart ssh
```

If you got errors about the locale when logging in run this:

```
sudo sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
sudo locale-gen
sudo update-locale LANG=en_US.UTF-8
```

Install node:

```
sudo apt update && sudo apt install -y curl
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
node --version
```

And `pi`:

```
sudo npm install -g --ignore-scripts @earendil-works/pi-coding-agent
mkdir ~/.pi
ln -s ~/config/pi ~/.pi/agent
pi --version
```

Generate an SSH key:

```
ssh-keygen -t ed25519 -C "rancho"
```

Add the public key to GitHub. Install git and clone the repo:

```
sudo apt install git
git clone git@github.com:WebBurnout/config.git
```

Install neovim and get the config:

```
sudo apt install neovim
mkdir -p ~/.config
ln -s ~/config/nvim ~/.config/nvim
```

Install tailscal
```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then remove key expiry.

Setup git config
```
ln -s ~/config/gitconfig ~/.gitconfig
```

