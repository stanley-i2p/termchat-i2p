**Termchat-I2P** now supports **optional hybrid post-quantum** key exchange for live sessions.

* When started with --pq, the application combines the existing **classical key agreement with a post-quantum KEM and derives one final session key from both**, so the session only becomes ready after both parts complete.

* If --pq is not used, the application continues to use the **original classical** live-session encryption path.

## Building liboqs (Linux)

```bash
sudo apt update
sudo apt install -y git cmake ninja-build gcc g++ make libssl-dev

git clone --depth 1 https://github.com/open-quantum-safe/liboqs.git
cd liboqs
cmake -S . -B build -GNinja -DBUILD_SHARED_LIBS=ON
cmake --build build --parallel $(nproc)
sudo cmake --install build
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib
sudo ldconfig
```
Quick check:
```bash
python -c "import oqs; print(oqs.get_enabled_kem_mechanisms())"
```

## Python requirements


* Install UV
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
* Create a virtual environment with a recent Python version:
```bash
uv venv --python 3.14 i2p_env
```
* Activate it:
```bash
source i2p_env/bin/activate
```
* Installing Dependencies
```bash
uv pip install -r requirements.txt
uv pip install liboqs-python
```

## Running

```bash
python chat-python.py --pq [profile]
```

Note: it may give warning upon start due to version differences. Discregard it.


### Behavior:


* both peers with --pq → hybrid PQ + classical
* both peers without --pq → classical only
* one peer with --pq, one without → connection should fail / be closed

