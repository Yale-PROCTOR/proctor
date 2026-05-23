To build and run the container:

```bash
./download.sh
docker build -t proctor:latest .
docker run -it proctor:latest
```

Inside the container, to translate all test cases and execute test vectors:

```bash
cd PUBLIC-Test-Corpus-static
./translate_all.py
./deployment/scripts/github-actions/run_rust.sh --keep-going
```

---

To translate an individual test case:

```bash
./translate.sh Public-Tests/B01_organic/bin2hex_lib
```

To test an individual test case:

```bash
./deployment/scripts/github-actions/run_rust.sh -m Public-Tests/B01_organic/bin2hex_lib
```
