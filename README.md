Overview:
- The cpp files are implementations of multiconsumser, multiprovider queue: atomic::wait and mutex + conditional variables
- The python script `compare_runner.py` benchmarks these implementations. It produces tables and graphs, which are saved in the "data" directory. It also prints results to the console.

Required dependencies:
- `g++`
- `python`
- Python packages listed in `requirements.txt`

Usage:
1. Make sure dependencies are installed
2. Run `python compare_runner.py`
3. See graphs and tables in the new "data" directory
