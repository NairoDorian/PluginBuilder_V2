// RealtimeCHOP.cpp - see RealtimeCHOP.h for the structure this template demonstrates.

#include "RealtimeCHOP.h"
#include "td_cpu_guard.h"   // on the include path via td_plugin_optimize(... REALTIME)

#include <algorithm>
#include <cstdio>
#include <cstring>

namespace {
bool g_cpuOk = true;        // probed once per process in FillCHOPPluginInfo
using clk = std::chrono::steady_clock;
}

extern "C"
{

DLLEXPORT
void
FillCHOPPluginInfo(CHOP_PluginInfo* info)
{
	if (!info->setAPIVersion(CHOPCPlusPlusAPIVersion))
		return;
	// Compiled with /arch:AVX2: check the CPU before any instance runs AVX2 code, so a machine without
	// it gets an error string instead of an illegal-instruction crash inside TouchDesigner.
	g_cpuOk = td::cpuSupportsAVX2();

	info->customOPInfo.opType->setString("#__OP_TYPE__#");
	info->customOPInfo.opLabel->setString("#__OP_LABEL__#");
	info->customOPInfo.opIcon->setString("#__OP_ICON__#");
	info->customOPInfo.authorName->setString("#__OP_AUTHOR__#");
	info->customOPInfo.authorEmail->setString("#__OP_EMAIL__#");
	info->customOPInfo.opHelpURL->setString("#__OP_HELPURL__#");
	info->customOPInfo.minInputs = 1;
	info->customOPInfo.maxInputs = 1;
}

DLLEXPORT
CHOP_CPlusPlusBase*
CreateCHOPInstance(const OP_NodeInfo* info)
{
	return new RealtimeCHOP(info);
}

DLLEXPORT
void
DestroyCHOPInstance(CHOP_CPlusPlusBase* instance)
{
	delete static_cast<RealtimeCHOP*>(instance);
}

}

RealtimeCHOP::RealtimeCHOP(const OP_NodeInfo* info) : myNodeInfo(info)
{
	myCpuOk = g_cpuOk;
}

RealtimeCHOP::~RealtimeCHOP()
{
	myRunner.setAsync(false);   // join the worker before members it uses are destroyed
}

void
RealtimeCHOP::getGeneralInfo(CHOP_GeneralInfo* ginfo, const OP_Inputs*, void*)
{
	// A pure number cruncher: cook only when something downstream uses the output. Set cookEveryFrame
	// instead if the node must keep running (e.g. to keep a worker or its telemetry alive while unviewed).
	ginfo->cookEveryFrame = false;
	ginfo->cookEveryFrameIfAsked = true;
	ginfo->timeslice = false;
	ginfo->inputMatchIndex = 0;
}

bool
RealtimeCHOP::getOutputInfo(CHOP_OutputInfo* info, const OP_Inputs* inputs, void*)
{
	// The one place parameters are read, once per cook. execute() reuses the snapshot.
	myParams = Parameters::eval(inputs);
	myParamsFresh = true;
	const OP_CHOPInput* in = inputs->getNumInputs() > 0 ? inputs->getInputCHOP(0) : nullptr;
	info->numChannels = (in && in->numChannels > 0) ? in->numChannels : 1;
	info->numSamples = 1;
	info->startIndex = 0;
	const OP_TimeInfo* ti = inputs->getTimeInfo();
	info->sampleRate = static_cast<float>(ti && ti->rate > 0.0 ? ti->rate : 60.0);
	return true;
}

void
RealtimeCHOP::getChannelName(int32_t index, OP_String* name, const OP_Inputs* inputs, void*)
{
	const OP_CHOPInput* in = inputs->getNumInputs() > 0 ? inputs->getInputCHOP(0) : nullptr;
	name->setString(in && index < in->numChannels ? in->getChannelName(index) : "level");
}

void
RealtimeCHOP::execute(CHOP_Output* output, const OP_Inputs* inputs, void*)
{
	++myExecuteCount;
	if (!output || !output->channels) return;
	if (!myCpuOk) {                                     // no AVX2: silence, and getErrorString says why
		for (int c = 0; c < output->numChannels; ++c) std::memset(output->channels[c], 0, output->numSamples * sizeof(float));
		return;
	}
	const auto t0 = clk::now();
	if (!myParamsFresh) myParams = Parameters::eval(inputs);   // execute without getOutputInfo: allowed by the API
	myParamsFresh = false;
	const Parameters::Values& p = myParams;

	myRunner.setAsync(p.async);                         // no-op unless the toggle changed

	const OP_CHOPInput* in = inputs->getNumInputs() > 0 ? inputs->getInputCHOP(0) : nullptr;
	const size_t nch = in ? static_cast<size_t>(std::max(0, in->numChannels)) : 0;
	const size_t n = in ? static_cast<size_t>(std::max(0, in->numSamples)) : 0;
	if (myIn.size() < nch) myIn.resize(nch);           // grows once
	for (size_t c = 0; c < nch; ++c) myIn[c] = in->getChannelData(static_cast<int32_t>(c));

	rt::Job& job = myRunner.jobSlot();
	job.seq = ++mySeq;
	const OP_TimeInfo* ti = inputs->getTimeInfo();
	job.dtMs = (ti && ti->deltaMS > 0.0 && ti->deltaMS < 5000.0) ? ti->deltaMS : 1000.0 / 60.0;
	job.reset = myResetPending;
	myResetPending = false;
	job.params.peak = p.mode == Parameters::Mode::Peak;
	job.params.gain = p.gain;
	job.params.smooth = p.smooth;
	job.params.attackMs = p.attackMs;
	job.params.releaseMs = p.releaseMs;
	job.fill(myIn.data(), nch, n);
	myRunner.submit();                                  // sync: processed now; async: picked up by the worker

	const rt::Result& r = myRunner.latest();            // async: usually the previous cook's result
	myProcessUs = r.processUs;
	for (int c = 0; c < output->numChannels; ++c) {
		output->channels[c][0] = (static_cast<size_t>(c) < r.levels.size()) ? r.levels[c] : 0.0f;
	}
	myCookUs = std::chrono::duration<double, std::micro>(clk::now() - t0).count();
}

int32_t
RealtimeCHOP::getNumInfoCHOPChans(void*)
{
	return 4;
}

void
RealtimeCHOP::getInfoCHOPChan(int index, OP_InfoCHOPChan* chan, void*)
{
	switch (index) {
	case 0: chan->name->setString("cook_time_us"); chan->value = static_cast<float>(myCookUs); break;
	case 1: chan->name->setString("process_time_us"); chan->value = static_cast<float>(myProcessUs); break;
	case 2: chan->name->setString("async_active"); chan->value = myRunner.async() ? 1.0f : 0.0f; break;
	case 3: chan->name->setString("jobs_dropped"); chan->value = static_cast<float>(myRunner.dropped()); break;
	default: break;
	}
}

// The info chain runs inside every cook: format the rows at most 4x per second and hand the same
// strings over in between, instead of building them on every call.
void
RealtimeCHOP::renderInfo()
{
	const auto now = clk::now();
	if (myInfoAt.time_since_epoch().count() != 0 && std::chrono::duration<double, std::milli>(now - myInfoAt).count() < 250.0) return;
	myInfoAt = now;
	char b[128];
	myRowName[0] = "execute_count"; snprintf(b, sizeof(b), "%d", myExecuteCount); myRowValue[0] = b;
	myRowName[1] = "cook_time"; snprintf(b, sizeof(b), "%.2f us", myCookUs); myRowValue[1] = b;
	myRowName[2] = "process_time"; snprintf(b, sizeof(b), "%.2f us (%s)", myProcessUs, myRunner.async() ? "worker thread" : "cook thread"); myRowValue[2] = b;
	myRowName[3] = "jobs_dropped"; snprintf(b, sizeof(b), "%llu", static_cast<unsigned long long>(myRunner.dropped())); myRowValue[3] = b;
	myRowName[4] = "simd"; myRowValue[4] = myCpuOk ? "AVX2 available" : "UNSUPPORTED CPU (no AVX2/FMA)";
}

bool
RealtimeCHOP::getInfoDATSize(OP_InfoDATSize* infoSize, void*)
{
	renderInfo();
	infoSize->rows = kInfoRows;
	infoSize->cols = 2;
	infoSize->byColumn = false;
	return true;
}

void
RealtimeCHOP::getInfoDATEntries(int32_t index, int32_t, OP_InfoDATEntries* entries, void*)
{
	if (index < 0 || index >= kInfoRows) return;
	entries->values[0]->setString(myRowName[index].c_str());
	entries->values[1]->setString(myRowValue[index].c_str());
}

void
RealtimeCHOP::getErrorString(OP_String* error, void*)
{
	if (!myCpuOk) error->setString("This CPU has no AVX2/FMA; the plugin is compiled for AVX2 and outputs silence.");
}

void
RealtimeCHOP::setupParameters(OP_ParameterManager* manager, void*)
{
	Parameters::setup(manager);
}

void
RealtimeCHOP::setParameterEnableStates(const OP_Inputs* inputs, OP_ParEnableState* state, void*)
{
	Parameters::setEnableStates(inputs, state);
}

void
RealtimeCHOP::pulsePressed(const char* name, void*)
{
	// Only a flag: the state it clears belongs to whichever thread runs the meter.
	if (!std::strcmp(name, Parameters::ResetName)) myResetPending = true;
}
