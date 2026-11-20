// lastbook-llm: runs one local language model for the Lastbook app. It talks only through its standard input and output,
// one JSON object per line, and never opens a socket, a port or a web address. It reads one file, the model it is given.
//
//   lastbook-llm --model FILE [--draft FILE] [--draft-max N] [--ctx N] [--threads N] [--cpu-only] [--measure]
//
// --draft names the model's multi-token-prediction drafter (Gemma 4's "mtp-*.gguf", a few hundred MB beside the model). With it
// the drafter proposes up to --draft-max tokens (default 3) and the model checks them in one step: the answer is the same
// tokens the model would have written alone (each is sampled from the model and a draft is kept only while it matches), only
// sooner. llama.cpp's common/speculative.cpp does the drafting. If the drafter cannot be loaded the model runs without it.
//
// On start it fits the model to this computer's memory with llama.cpp's own fitting (common/fit.cpp, the same as llama-server's
// default --fit), prints {"ev":"fit",...} with what it measured, then loads the model and prints {"ev":"ready",...}.
// --measure stops after the fit line and loads nothing.
//
// Requests, one a line on standard input:
//   {"op":"gen","id":1,"segments":[["<|turn>",true],["text",false],...],"max_tokens":1800,"temp":0.2,"top_k":64,"top_p":0.95,
//    "min_p":0.05,"seed":1,"stop":["<|tool_response>"]}
//       A segment marked true is template markup and its special tokens are read as such. Text marked false never is: a document
//       holding "<|turn>" stays plain text and cannot start a turn of its own.
//   {"op":"cancel"}   stops the answer being written
//   {"op":"quit"}     or the end of standard input: leave
// Answers: {"ev":"tok","id":1,"t":"..."} for each piece, then {"ev":"done","id":1,"reason":"eog|stop|length|cancel",...}
// or {"ev":"error","id":1,"code":"...","msg":"..."}. Everything else, llama.cpp's own log included, goes to standard error.

#include "llama.h"
#include "ggml-backend.h"
#include "common.h"
#include "fit.h"
#include "log.h"
#include "sampling.h"
#include "speculative.h"

#include <nlohmann/json.hpp>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <fcntl.h>
#  include <io.h>
#elif defined(__APPLE__)
#  include <unistd.h>
#  include <sys/sysctl.h>
#  include <mach/mach.h>
#else
#  include <unistd.h>
#  include <sys/sysinfo.h>
#endif

using json = nlohmann::ordered_json;

// common.cpp is built in for the drafting (speculative.cpp needs its batch, sampler and checkpoint helpers), without the parts of
// llama.cpp's common library that download (arg.cpp, download.cpp, hf-cache.cpp are not built: the build checks for socket calls).

static FILE * g_out = nullptr;      // the protocol: the real standard output, kept apart from anything a library prints
static std::mutex g_out_lock;

static void send(const json & j) {
    std::string line = j.dump(-1, ' ', false, json::error_handler_t::replace) + "\n";
    std::lock_guard<std::mutex> lk(g_out_lock);
    fwrite(line.data(), 1, line.size(), g_out);
    fflush(g_out);
}

static void log_to_stderr(ggml_log_level level, const char * text, void *) {
    if (level >= GGML_LOG_LEVEL_INFO || level == GGML_LOG_LEVEL_CONT) {
        fputs(text, stderr);
    }
}

static uint64_t ram_total() {
#ifdef _WIN32
    MEMORYSTATUSEX m = { sizeof(m) };
    return GlobalMemoryStatusEx(&m) ? m.ullTotalPhys : 0;
#elif defined(__APPLE__)
    uint64_t v = 0;
    size_t n = sizeof(v);
    return sysctlbyname("hw.memsize", &v, &n, nullptr, 0) == 0 ? v : 0;
#else
    struct sysinfo si;
    return sysinfo(&si) == 0 ? (uint64_t) si.totalram * si.mem_unit : 0;
#endif
}

static uint64_t ram_free() {   // what could be had without pushing other programs out: free plus what the system would drop
#ifdef _WIN32
    MEMORYSTATUSEX m = { sizeof(m) };
    return GlobalMemoryStatusEx(&m) ? m.ullAvailPhys : 0;
#elif defined(__APPLE__)
    vm_statistics64_data_t s;
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    if (host_statistics64(mach_host_self(), HOST_VM_INFO64, (host_info64_t) &s, &count) != KERN_SUCCESS) {
        return 0;
    }
    return (uint64_t) (s.free_count + s.inactive_count + s.purgeable_count) * (uint64_t) vm_page_size;
#else
    FILE * f = fopen("/proc/meminfo", "r");     // Linux's own estimate of the same: MemAvailable
    unsigned long long kb = 0;
    char line[256];
    while (f && fgets(line, sizeof(line), f)) {
        if (sscanf(line, "MemAvailable: %llu kB", &kb) == 1) {
            break;
        }
    }
    if (f) {
        fclose(f);
    }
    return (uint64_t) kb * 1024;
#endif
}

// ------------------------------------------------------------------------------------------------- requests from the app --
struct Inbox {
    std::mutex m;
    std::condition_variable cv;
    std::deque<json> q;
    std::atomic<bool> cancel{false};
    std::atomic<bool> quit{false};
};

static void read_requests(Inbox * in) {
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) {
            continue;
        }
        json j = json::parse(line, nullptr, false);
        if (j.is_discarded() || !j.is_object()) {
            send({{"ev", "error"}, {"code", "bad_request"}, {"msg", "not a JSON object"}});
            continue;
        }
        const std::string op = j.value("op", "");
        if (op == "cancel") {
            in->cancel = true;
            continue;
        }
        if (op == "quit") {
            break;
        }
        std::lock_guard<std::mutex> lk(in->m);
        in->q.push_back(std::move(j));
        in->cv.notify_one();
    }
    in->quit = true;      // the app closed the pipe or asked us to go: whatever is running stops too
    in->cancel = true;
    in->cv.notify_one();
}

// ------------------------------------------------------------------------------------------------------------ generation --
struct Engine {
    llama_model * model = nullptr;
    llama_context * ctx = nullptr;
    const llama_vocab * vocab = nullptr;
    std::vector<llama_token> cached;   // the tokens the context holds, in order
    Inbox * in = nullptr;
    // multi-token prediction (--draft): the drafter, its context (sharing the model's memory) and llama.cpp's drafting state
    llama_model * dmodel = nullptr;
    llama_context * dctx = nullptr;
    common_speculative * spec = nullptr;
    int n_draft = 3;
};

static std::vector<llama_token> tokenize(const llama_vocab * vocab, const std::string & text, bool special) {
    std::vector<llama_token> out(text.size() + 8);
    int n = llama_tokenize(vocab, text.data(), (int32_t) text.size(), out.data(), (int32_t) out.size(), false, special);
    if (n < 0) {
        out.resize(-n);
        n = llama_tokenize(vocab, text.data(), (int32_t) text.size(), out.data(), (int32_t) out.size(), false, special);
    }
    out.resize(n > 0 ? n : 0);
    return out;
}

static std::string piece(const llama_vocab * vocab, llama_token t) {
    char buf[256];
    int n = llama_token_to_piece(vocab, t, buf, sizeof(buf), 0, true);
    if (n < 0) {
        std::string big(-n, '\0');
        n = llama_token_to_piece(vocab, t, big.data(), (int32_t) big.size(), 0, true);
        return big.substr(0, n > 0 ? n : 0);
    }
    return std::string(buf, n);
}

static size_t complete_utf8(const std::string & s) {   // how much of s ends on a whole character; the rest waits for the next piece
    size_t n = s.size();
    for (size_t back = 1; back <= 4 && back <= n; back++) {
        unsigned char c = (unsigned char) s[n - back];
        if ((c & 0xC0) == 0x80) {
            continue;                      // a continuation byte: look further back for the lead
        }
        size_t need = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 1;
        return back >= need ? n : n - back;
    }
    return n;
}

static bool decode_all(Engine & e, const std::vector<llama_token> & toks, size_t from, int & err) {
    const int n_batch = (int) llama_n_batch(e.ctx);
    for (size_t i = from; i < toks.size(); i += n_batch) {
        if (e.in->cancel) {
            err = 2;
            return false;
        }
        int n = (int) std::min<size_t>(n_batch, toks.size() - i);
        int rc = llama_decode(e.ctx, llama_batch_get_one(const_cast<llama_token *>(toks.data() + i), n));
        if (rc != 0) {
            err = rc == 2 ? 2 : 1;    // 2: aborted by our callback
            return false;
        }
        e.cached.insert(e.cached.end(), toks.begin() + i, toks.begin() + i + n);
    }
    return true;
}


// The model and its drafter (--draft): read the prompt but its last token, then repeat: the drafter proposes up to n_draft
// tokens, the model reads the last token and the proposals in one batch, and the tokens it samples are kept up to the first
// that differs from a proposal. This follows llama.cpp's examples/speculative-simple, with the same stops, cancel and output
// as generate() below.
static std::vector<std::string> stop_list(const json & req) {
    std::vector<std::string> stops;
    for (const auto & s : req.value("stop", json::array())) {
        if (s.is_string()) {
            stops.push_back(s.get<std::string>());
        }
    }
    return stops;
}

static void generate_drafted(Engine & e, const json & req, const std::vector<llama_token> & toks, size_t keep, size_t reused,
                             std::chrono::steady_clock::time_point t0) {
    using clock = std::chrono::steady_clock;
    const json id = req.value("id", json());
    auto fail = [&](const char * code, const std::string & msg) {
        send({{"ev", "error"}, {"id", id}, {"code", code}, {"msg", msg}});
    };
    llama_memory_t mem = llama_get_memory(e.ctx);
    const int n_ctx = (int) llama_n_ctx(e.ctx);
    const int n_batch = (int) llama_n_batch(e.ctx);
    int max_tokens = std::min(std::max(1, req.value("max_tokens", 1024)), n_ctx - (int) toks.size());
    const llama_seq_id seq = 0;

    // the prompt, all but its last token, in batches the drafter also sees
    common_batch batch(e.ctx);
    for (size_t i = keep; i + 1 < toks.size(); i += n_batch) {
        if (e.in->cancel) {
            llama_memory_clear(mem, true);
            e.cached.clear();
            send({{"ev", "done"}, {"id", id}, {"reason", "cancel"}, {"n_prompt", toks.size()}, {"n_reused", reused}, {"n_gen", 0}});
            return;
        }
        batch.clear();
        const size_t end = std::min(toks.size() - 1, i + (size_t) n_batch);
        for (size_t k = i; k < end; k++) {
            batch.add(toks[k], (llama_pos) k, seq, false);
        }
        if (llama_process(e.ctx, LLAMA_PROCESS_TYPE_DECODE, batch.get()) != 0 || !common_speculative_process(e.spec, batch)) {
            llama_memory_clear(mem, true);
            e.cached.clear();
            return fail("decode", "the model could not read the question (llama_decode failed)");
        }
        e.cached.insert(e.cached.end(), toks.begin() + i, toks.begin() + end);
    }
    const auto t1 = clock::now();

    common_params_sampling sp;            // the same sampling as generate(): top-k, top-p, min-p, temperature, seed
    sp.top_k = req.value("top_k", 64);
    sp.top_p = req.value("top_p", 0.95f);
    sp.min_p = req.value("min_p", 0.05f);
    sp.temp = req.value("temp", 0.2f);
    sp.seed = req.value("seed", (uint32_t) LLAMA_DEFAULT_SEED);
    sp.samplers = {COMMON_SAMPLER_TYPE_TOP_K, COMMON_SAMPLER_TYPE_TOP_P, COMMON_SAMPLER_TYPE_MIN_P, COMMON_SAMPLER_TYPE_TEMPERATURE};
    common_sampler_ptr smpl(common_sampler_init(e.model, sp));

    const bool ckpt_tgt = common_context_can_seq_rm(e.ctx) == COMMON_CONTEXT_SEQ_RM_TYPE_FULL;
    const bool ckpt_dft = common_context_can_seq_rm(e.dctx) == COMMON_CONTEXT_SEQ_RM_TYPE_FULL;
    llama_tokens prompt(toks.begin(), toks.end() - 1);
    llama_token last = toks.back();
    int n_past = (int) prompt.size();
    common_speculative_begin(e.spec, seq, prompt);

    const std::vector<std::string> stops = stop_list(req);
    std::string pending, why = "length", stopped_by;
    int n_gen = 0, n_drafted = 0, n_accepted = 0;
    llama_tokens draft;
    common_prompt_checkpoint ckpt;
    bool done = false;
    while (!done) {
        if (e.in->cancel) {
            why = "cancel";
            break;
        }
        if (draft.empty()) {
            ckpt.update_pos((int64_t) prompt.size(), llama_memory_seq_pos_min(mem, seq), llama_memory_seq_pos_max(mem, seq));
            if (ckpt_dft) {
                ckpt.update_dft(e.dctx, seq, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
            }
            const int n_max = std::max(0, std::min(n_ctx - n_past - 2, max_tokens - n_gen - 1));
            common_speculative_get_draft_params(e.spec, seq) = {true, n_max, n_past, last, &prompt, &draft};
            if (!getenv("LBNODRAFT")) common_speculative_draft(e.spec);
            if (!draft.empty() && ckpt_tgt) {
                ckpt.update_tgt(e.ctx, seq, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
            }
            if (ckpt_dft) {
                ckpt.load_dft(e.dctx, seq, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
            }
            llama_memory_seq_rm(llama_get_memory(e.dctx), seq, ckpt.pos_max + 1, -1);
        }
        batch.clear();
        batch.add(last, n_past++, seq, true);
        for (size_t i = 0; i < draft.size(); i++) {
            batch.add(draft[i], n_past + (llama_pos) i, seq, true);
        }
        if (llama_process(e.ctx, LLAMA_PROCESS_TYPE_DECODE, batch.get()) != 0 || !common_speculative_process(e.spec, batch)) {
            why = e.in->cancel ? "cancel" : "decode";
            break;
        }
        common_sampler_ptr saved;
        if (ckpt_tgt) {
            saved.reset(common_sampler_clone(smpl.get()));
        }
        const size_t n_draft = draft.size();
        auto ids = common_sampler_sample_and_accept_n(smpl.get(), e.ctx, draft);
        if (ckpt_tgt && ids.size() - 1 < n_draft) {   // this memory cannot drop the refused proposals: back to before them
            draft = std::move(ids);
            ckpt.load_tgt(e.ctx, seq, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
            llama_memory_seq_rm(mem, seq, ckpt.pos_max + 1, -1);
            ckpt.load_dft(e.dctx, seq, LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
            llama_memory_seq_rm(llama_get_memory(e.dctx), seq, ckpt.pos_max + 1, -1);
            prompt.resize(ckpt.n_tokens);
            smpl = std::move(saved);
            n_past = (int) prompt.size();
            continue;
        }
        if (getenv("LBDEBUG") && n_gen < 12) {
            fprintf(stderr, "DBG n_past=%d last=%d draft=[", n_past, last);
            for (auto t : draft) fprintf(stderr, "%d ", t);
            fprintf(stderr, "] ids=[");
            for (auto t : ids) fprintf(stderr, "%d '%s' ", t, piece(e.vocab, t).c_str());
            fprintf(stderr, "] ckpt_tgt=%d ckpt_dft=%d\n", (int) ckpt_tgt, (int) ckpt_dft);
        }
        common_speculative_accept(e.spec, seq, (uint16_t) (ids.size() - 1));
        n_past += (int) ids.size() - 1;
        n_drafted += (int) n_draft;
        n_accepted += (int) ids.size() - 1;
        for (size_t i = 0; i < ids.size(); i++) {
            prompt.push_back(last);
            last = ids[i];
            if (llama_vocab_is_eog(e.vocab, last)) {
                why = "eog";
                done = true;
                break;
            }
            std::string p = piece(e.vocab, last);
            if (std::find(stops.begin(), stops.end(), p) != stops.end()) {
                why = "stop";
                stopped_by = p;
                done = true;
                break;
            }
            pending += p;
            size_t ok = complete_utf8(pending);
            if (ok) {
                send({{"ev", "tok"}, {"id", id}, {"t", pending.substr(0, ok)}});
                pending.erase(0, ok);
            }
            if (++n_gen >= max_tokens) {
                why = "length";
                done = true;
                break;
            }
        }
        draft.clear();
        llama_memory_seq_rm(mem, seq, n_past, -1);
        llama_memory_seq_rm(llama_get_memory(e.dctx), seq, n_past, -1);
    }
    if (!pending.empty()) {
        send({{"ev", "tok"}, {"id", id}, {"t", pending}});
    }
    // What the memory holds now is the prompt and the accepted tokens before the last one: the next request reuses it.
    llama_memory_seq_rm(mem, seq, (llama_pos) prompt.size(), -1);
    e.cached.assign(prompt.begin(), prompt.end());
    if (why == "decode") {
        llama_memory_clear(mem, true);
        e.cached.clear();
        return fail("decode", "the model stopped while writing (llama_decode failed)");
    }
    const auto t2 = clock::now();
    auto ms = [](auto a, auto b) { return (long long) std::chrono::duration_cast<std::chrono::milliseconds>(b - a).count(); };
    json out = {{"ev", "done"}, {"id", id}, {"reason", why}, {"n_prompt", toks.size()}, {"n_reused", reused}, {"n_gen", n_gen},
                {"n_drafted", n_drafted}, {"n_accepted", n_accepted}, {"prompt_ms", ms(t0, t1)}, {"gen_ms", ms(t1, t2)}};
    if (!stopped_by.empty()) {
        out["stop"] = stopped_by;
    }
    send(out);
}

static void generate(Engine & e, const json & req) {
    using clock = std::chrono::steady_clock;
    const auto t0 = clock::now();
    const json id = req.value("id", json());
    auto fail = [&](const char * code, const std::string & msg) {
        send({{"ev", "error"}, {"id", id}, {"code", code}, {"msg", msg}});
    };
    std::vector<llama_token> toks;
    if (llama_vocab_get_add_bos(e.vocab)) {
        toks.push_back(llama_vocab_bos(e.vocab));
    }
    for (const auto & seg : req.value("segments", json::array())) {
        if (!seg.is_array() || seg.size() != 2 || !seg[0].is_string() || !seg[1].is_boolean()) {
            return fail("bad_request", "segments are [text, special] pairs");
        }
        auto part = tokenize(e.vocab, seg[0].get<std::string>(), seg[1].get<bool>());
        toks.insert(toks.end(), part.begin(), part.end());
    }
    const int n_ctx = (int) llama_n_ctx(e.ctx);
    int max_tokens = std::max(1, req.value("max_tokens", 1024));
    if ((int) toks.size() + 16 >= n_ctx) {
        return fail("too_long", "the question and its sources are " + std::to_string(toks.size()) + " tokens; the model holds " + std::to_string(n_ctx));
    }
    max_tokens = std::min(max_tokens, n_ctx - (int) toks.size());

    // Reuse what the context already holds: a follow-up round repeats the conversation so far.
    size_t keep = 0;
    while (keep < e.cached.size() && keep < toks.size() && e.cached[keep] == toks[keep]) {
        keep++;
    }
    if (keep == toks.size()) {
        keep--;                            // the last prompt token is decoded again, for its logits
    }
    if (e.spec && toks.size() >= 2 && keep > toks.size() - 2) {
        keep = toks.size() - 2;            // drafting: at least one token is read here, so the drafter has the state it ends on
    }
    llama_memory_t mem = llama_get_memory(e.ctx);
    if (keep < e.cached.size()) {
        if (!llama_memory_seq_rm(mem, 0, (llama_pos) keep, -1)) {
            llama_memory_clear(mem, true);   // the sliding-window layers cannot always go back: start over
            keep = 0;
        }
        e.cached.resize(keep);
    }
    const size_t reused = keep;

    e.in->cancel = false;
    if (e.spec && toks.size() >= 2) {
        return generate_drafted(e, req, toks, keep, reused, t0);
    }
    int err = 0;
    if (!decode_all(e, toks, keep, err)) {
        llama_memory_clear(mem, true);
        e.cached.clear();
        if (err == 2) {
            send({{"ev", "done"}, {"id", id}, {"reason", "cancel"}, {"n_prompt", toks.size()}, {"n_reused", reused}, {"n_gen", 0}});
            return;
        }
        return fail("decode", "the model could not read the question (llama_decode failed)");
    }
    const auto t1 = clock::now();

    llama_sampler * smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(smpl, llama_sampler_init_top_k(req.value("top_k", 64)));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_p(req.value("top_p", 0.95f), 1));
    llama_sampler_chain_add(smpl, llama_sampler_init_min_p(req.value("min_p", 0.05f), 1));
    llama_sampler_chain_add(smpl, llama_sampler_init_temp(req.value("temp", 0.2f)));
    llama_sampler_chain_add(smpl, llama_sampler_init_dist(req.value("seed", (uint32_t) LLAMA_DEFAULT_SEED)));

    std::vector<std::string> stops;
    for (const auto & s : req.value("stop", json::array())) {
        if (s.is_string()) {
            stops.push_back(s.get<std::string>());
        }
    }
    std::string pending, why = "length", stopped_by;
    int n_gen = 0;
    for (; n_gen < max_tokens; n_gen++) {
        if (e.in->cancel) {
            why = "cancel";
            break;
        }
        llama_token t = llama_sampler_sample(smpl, e.ctx, -1);
        if (llama_vocab_is_eog(e.vocab, t)) {
            why = "eog";
            break;
        }
        std::string p = piece(e.vocab, t);
        bool hit = false;
        for (const auto & s : stops) {
            if (p == s) {
                hit = true;
                stopped_by = s;
            }
        }
        if (hit) {
            why = "stop";
            break;
        }
        pending += p;
        size_t ok = complete_utf8(pending);
        if (ok) {
            send({{"ev", "tok"}, {"id", id}, {"t", pending.substr(0, ok)}});
            pending.erase(0, ok);
        }
        llama_token one = t;
        if (llama_decode(e.ctx, llama_batch_get_one(&one, 1)) != 0) {
            why = e.in->cancel ? "cancel" : "decode";
            break;
        }
        e.cached.push_back(t);
    }
    llama_sampler_free(smpl);
    if (!pending.empty()) {
        send({{"ev", "tok"}, {"id", id}, {"t", pending}});
    }
    if (why == "decode") {
        llama_memory_clear(mem, true);
        e.cached.clear();
        return fail("decode", "the model stopped while writing (llama_decode failed)");
    }
    const auto t2 = clock::now();
    auto ms = [](auto a, auto b) { return (long long) std::chrono::duration_cast<std::chrono::milliseconds>(b - a).count(); };
    json done = {{"ev", "done"}, {"id", id}, {"reason", why}, {"n_prompt", toks.size()}, {"n_reused", reused}, {"n_gen", n_gen},
                 {"prompt_ms", ms(t0, t1)}, {"gen_ms", ms(t1, t2)}};
    if (!stopped_by.empty()) {
        done["stop"] = stopped_by;
    }
    send(done);
}

// ------------------------------------------------------------------------------------------------------------------ start --
static json rows_json(const std::vector<common_memory_breakdown_row> & rows, uint64_t & device_bytes, uint64_t & host_bytes) {
    json out = json::array();
    device_bytes = host_bytes = 0;
    for (const auto & r : rows) {
        if (r.kind == common_memory_breakdown_row::BUFFER_TYPE) {
            continue;
        }
        const uint64_t used = r.model + r.context + r.compute;
        json j = {{"kind", r.kind == common_memory_breakdown_row::HOST ? "host" : "device"}, {"name", r.name}, {"desc", r.description},
                  {"model", r.model}, {"context", r.context}, {"compute", r.compute}};
        if (r.kind == common_memory_breakdown_row::DEVICE) {
            j["total"] = r.total;
            j["free"] = r.free;
            device_bytes += used;
        } else {
            host_bytes += used;
        }
        out.push_back(j);
    }
    return out;
}

static bool on_abort(void * data) {
    return ((Inbox *) data)->cancel.load();
}

static bool on_progress(float p, void * data) {
    static int last = -1;
    int pct = (int) (p * 100);
    if (pct / 5 != last / 5) {
        last = pct;
        send({{"ev", "progress"}, {"p", pct}});
    }
    return !((Inbox *) data)->quit.load();
}

int main(int argc, char ** argv) {
    // The protocol keeps the real standard output to itself. File descriptor 1 then points at standard error, so a library
    // that prints can never break a line of JSON.
#ifdef _WIN32
    int proto = _dup(1);
    _dup2(2, 1);
    _setmode(proto, _O_BINARY);
    _setmode(_fileno(stdin), _O_BINARY);
    g_out = _fdopen(proto, "wb");
#else
    int proto = dup(1);
    dup2(2, 1);
    g_out = fdopen(proto, "w");
#endif
    std::string path, draft_path;
    int n_ctx = 16384, threads = 0, n_draft = 3;
    bool cpu_only = false, measure = false;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--model" && i + 1 < argc) {
            path = argv[++i];
        } else if (a == "--draft" && i + 1 < argc) {
            draft_path = argv[++i];
        } else if (a == "--draft-max" && i + 1 < argc) {
            n_draft = std::max(1, std::min(16, std::atoi(argv[++i])));
        } else if (a == "--ctx" && i + 1 < argc) {
            n_ctx = std::atoi(argv[++i]);
        } else if (a == "--threads" && i + 1 < argc) {
            threads = std::atoi(argv[++i]);
        } else if (a == "--cpu-only") {
            cpu_only = true;
        } else if (a == "--measure") {
            measure = true;
        } else {
            send({{"ev", "error"}, {"code", "usage"}, {"msg", "unknown option " + a}});
            return 2;
        }
    }
    if (path.empty()) {
        send({{"ev", "error"}, {"code", "usage"}, {"msg", "--model FILE is required"}});
        return 2;
    }
    llama_log_set(log_to_stderr, nullptr);
    common_log_set_verbosity_thold(LOG_LEVEL_WARN);
    ggml_backend_load_all();               // the backends beside this program, when they are separate files (Windows)
    llama_backend_init();

    Inbox in;
    llama_model_params mp = llama_model_default_params();
    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = n_ctx;
    cp.n_batch = 2048;
    cp.n_ubatch = 512;
    cp.n_seq_max = 1;
    if (threads > 0) {
        cp.n_threads = cp.n_threads_batch = threads;
    }
    cp.abort_callback = on_abort;
    cp.abort_callback_data = &in;
    cp.no_perf = true;
    cp.swa_full = false;   // as llama-server runs it: the sliding-window layers keep only their window, not the whole context
    if (!draft_path.empty()) {   // checking proposals reads several outputs per step (common_speculative_get_output_limits)
        const auto lim = common_speculative_get_output_limits((int32_t) cp.n_batch, 1, n_draft);
        cp.n_outputs_max = (uint32_t) lim.total;
        cp.n_outputs_max_per_seq = (uint32_t) lim.per_seq;
    }
    static ggml_backend_dev_t no_devices[1] = { nullptr };
    if (cpu_only) {
        mp.devices = no_devices;
        mp.n_gpu_layers = 0;
    }

    // Fit: only settings left at llama.cpp's defaults are changed, and the context stays the size asked for.
    std::vector<float> split(llama_max_devices(), 0.0f);
    std::vector<llama_model_tensor_buft_override> overrides(llama_max_tensor_buft_overrides());
    std::vector<size_t> margins(llama_max_devices(), (size_t) 1024 * 1024 * 1024);
    auto t_fit = std::chrono::steady_clock::now();
    common_params_fit_status st = COMMON_PARAMS_FIT_STATUS_SUCCESS;
    if (!cpu_only) {
        st = common_fit_params(path.c_str(), &mp, &cp, split.data(), overrides.data(), margins.data(), (uint32_t) n_ctx, nullptr, GGML_LOG_LEVEL_ERROR);
    }
    if (st == COMMON_PARAMS_FIT_STATUS_ERROR) {
        send({{"ev", "error"}, {"code", "model_file"}, {"msg", "llama.cpp could not read the model file"}});
        return 3;
    }
    json fit = {{"ev", "fit"}, {"status", st == COMMON_PARAMS_FIT_STATUS_SUCCESS ? "fits" : "does_not_fit"}, {"n_ctx", n_ctx},
                {"n_gpu_layers", mp.n_gpu_layers}, {"cpu_only", cpu_only}, {"ram_total", ram_total()}, {"ram_free", ram_free()}};
    int n_over = 0;
    for (const auto & o : overrides) {
        if (!o.pattern) {
            break;
        }
        n_over++;
    }
    fit["host_overrides"] = n_over;
    try {
        uint32_t n_train = 0, n_got = 0;
        uint64_t dev = 0, host = 0;
        fit["memory"] = rows_json(common_fit_measure(path.c_str(), &mp, &cp, &n_train, &n_got, GGML_LOG_LEVEL_ERROR), dev, host);
        fit["device_bytes"] = dev;
        fit["host_bytes"] = host;
    } catch (const std::exception & ex) {
        fit["measure_error"] = ex.what();
    }
    fit["fit_ms"] = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t_fit).count();
    send(fit);
    if (measure) {
        return 0;
    }

    std::thread reader(read_requests, &in);
    reader.detach();
    mp.progress_callback = on_progress;
    mp.progress_callback_user_data = &in;
    auto t_load = std::chrono::steady_clock::now();
    Engine e;
    e.in = &in;
    e.model = llama_model_load_from_file(path.c_str(), mp);
    if (!e.model) {
        send({{"ev", "error"}, {"code", in.quit ? "quit" : "load"}, {"msg", "the model could not be loaded"}});
        return 4;
    }
    e.ctx = llama_init_from_model(e.model, cp);
    if (!e.ctx) {
        send({{"ev", "error"}, {"code", "context"}, {"msg", "there was not enough memory for the model's working space"}});
        llama_model_free(e.model);
        return 5;
    }
    e.vocab = llama_model_get_vocab(e.model);
    json draft_info = nullptr;
    if (!draft_path.empty()) {
        // As common_speculative_init_result does for an MTP drafter: its own small model, a context of the MTP kind tied to the
        // model's (Gemma 4's drafter reads the model's memory), as long as the model's. Kept on the same devices as the model.
        llama_model_params dmp = llama_model_default_params();
        if (cpu_only) {
            dmp.devices = no_devices;
            dmp.n_gpu_layers = 0;
        }
        e.dmodel = llama_model_load_from_file(draft_path.c_str(), dmp);
        llama_context_params dcp = cp;
        dcp.ctx_type = LLAMA_CONTEXT_TYPE_MTP;
        dcp.n_ctx = llama_n_ctx(e.ctx);
        dcp.n_rs_seq = 0;
        dcp.ctx_other = e.ctx;
        dcp.n_outputs_max = 1;
        dcp.n_outputs_max_per_seq = 1;
        e.dctx = e.dmodel ? llama_init_from_model(e.dmodel, dcp) : nullptr;
        if (e.dctx) {
            common_params_speculative sp;
            sp.types = {COMMON_SPECULATIVE_TYPE_DRAFT_MTP};
            sp.draft.n_max = n_draft;
            sp.draft.ctx_tgt = e.ctx;
            sp.draft.ctx_dft = e.dctx;
            e.spec = common_speculative_init(sp, 1);
            e.n_draft = n_draft;
        }
        if (!e.spec) {     // the model alone, as without --draft
            if (e.dctx) {
                llama_free(e.dctx);
                e.dctx = nullptr;
            }
            if (e.dmodel) {
                llama_model_free(e.dmodel);
                e.dmodel = nullptr;
            }
        }
        draft_info = {{"used", e.spec != nullptr}, {"n_max", n_draft}};
    }
    char desc[256] = {0};
    llama_model_desc(e.model, desc, sizeof(desc));
    send({{"ev", "ready"}, {"n_ctx", llama_n_ctx(e.ctx)}, {"desc", desc}, {"threads", llama_n_threads(e.ctx)},
          {"load_ms", (long long) std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t_load).count()},
          {"ram_free", ram_free()}, {"draft", draft_info}});

    while (true) {
        json req;
        {
            std::unique_lock<std::mutex> lk(in.m);
            in.cv.wait(lk, [&] { return in.quit || !in.q.empty(); });
            if (in.quit) {
                break;
            }
            req = std::move(in.q.front());
            in.q.pop_front();
        }
        if (req.value("op", "") == "gen") {
            try {
                generate(e, req);
            } catch (const std::exception & ex) {
                send({{"ev", "error"}, {"id", req.value("id", json())}, {"code", "bad_request"}, {"msg", ex.what()}});
            }
        } else {
            send({{"ev", "error"}, {"code", "bad_request"}, {"msg", "unknown op"}});
        }
    }
    if (e.spec) {
        common_speculative_free(e.spec);
    }
    if (e.dctx) {
        llama_free(e.dctx);
    }
    if (e.dmodel) {
        llama_model_free(e.dmodel);
    }
    llama_free(e.ctx);
    llama_model_free(e.model);
    llama_backend_free();
    return 0;
}
