## DeadDrop Server

This is probably the most important part of TermchatI2P offline capabilities. We decided not to go usual route by using DHT models for a reason :)
Detailed explanation of duffusion propagation model is coming soon (we believe in testing first, before presenting anything worth reading :)).

At this stage, we have 2 versions of **DeadDrop** server written in python and in rust.

### Hosting and Setups

#### Python

We believe that people that want to host instances of **DeadDrop** servers in python do not need any instructions on how to setup.
Our team runs **DD** instances in **tmux** to have instant access to logging information.

#### Rust

* Dependencies and Setup

```bash
sudo apt install build-essential curl -y

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```
(Use Option 1 when installer asks).

```bash
source "$HOME/.cargo/env"
```

* DeadDrop Server project setup

    - Create project structure:

```bash
cargo new deaddrop-server-rust
```
(You can use any name you want. Make sure to match with Cargo.toml)

    - Overwrite **main.rs** and **Cargo.toml** with files provided in repo.

    - Build and execute

```bash
cargo build --release
cargo run --release
```
(If you want, you can **strip** binary as well)




 
