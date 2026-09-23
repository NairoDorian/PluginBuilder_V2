// RealtimeCHOP.h - a real-time-first CHOP skeleton (PluginBuilder template, not a Derivative sample).
//
// What it does: one output sample per input channel, the smoothed RMS or peak level of the channel's
// newest block. What it is FOR: the structure. Replace the meter in RealtimeCHOPCore.h with your own
// processing and keep the shape:
//
//   getOutputInfo   reads every parameter ONCE per cook (generated Parameters::eval) - nothing else
//                   in the cook touches the parameter system
//   execute         copies the input into a preallocated job, runs it (inline, or hands it to the
//                   worker through a wait-free triple buffer), copies the newest result out.
//                   No allocation, lock or system call after the first cooks.
//   info callbacks  hand over strings formatted at most 4x per second, never per call
//   setParameterEnableStates  greys out inactive parameters (generated from parameters.json)
//
// Edit parameters.json (project root) and pulse "Generate Parameters" on the PluginBuilder COMP (or run
// dev/gen_parameters.py) to regenerate Parameters.h/.cpp.
#pragma once

#include "CHOP_CPlusPlusBase.h"
#include "Parameters.h"
#include "RealtimeCHOPCore.h"

#include <chrono>
#include <string>

using namespace TD;

class RealtimeCHOP : public CHOP_CPlusPlusBase
{
public:
	RealtimeCHOP(const OP_NodeInfo* info);
	virtual ~RealtimeCHOP();

	virtual void getGeneralInfo(CHOP_GeneralInfo*, const OP_Inputs*, void*) override;
	virtual bool getOutputInfo(CHOP_OutputInfo*, const OP_Inputs*, void*) override;
	virtual void getChannelName(int32_t index, OP_String* name, const OP_Inputs*, void* reserved1) override;
	virtual void execute(CHOP_Output*, const OP_Inputs*, void* reserved) override;

	virtual int32_t getNumInfoCHOPChans(void* reserved1) override;
	virtual void getInfoCHOPChan(int index, OP_InfoCHOPChan* chan, void* reserved1) override;
	virtual bool getInfoDATSize(OP_InfoDATSize* infoSize, void* reserved1) override;
	virtual void getInfoDATEntries(int32_t index, int32_t nEntries, OP_InfoDATEntries* entries, void* reserved1) override;
	virtual void getErrorString(OP_String* error, void* reserved1) override;

	virtual void setupParameters(OP_ParameterManager* manager, void* reserved1) override;
	virtual void setParameterEnableStates(const OP_Inputs* inputs, OP_ParEnableState* state, void* reserved1) override;
	virtual void pulsePressed(const char* name, void* reserved1) override;

private:
	void renderInfo();                                  // formats the Info DAT rows (<= 4 Hz)

	const OP_NodeInfo* myNodeInfo;
	bool myCpuOk{ true };                               // AVX2/FMA present (td_cpu_guard.h)
	Parameters::Values myParams;                        // this cook's parameters (getOutputInfo)
	bool myParamsFresh{ false };
	rt::Runner myRunner;
	std::vector<const float*> myIn;                     // input channel pointers (grows once)
	uint64_t mySeq{ 0 };
	bool myResetPending{ false };
	int32_t myExecuteCount{ 0 };
	double myCookUs{ 0.0 };
	double myProcessUs{ 0.0 };

	static constexpr int kInfoRows = 5;
	std::string myRowName[kInfoRows], myRowValue[kInfoRows];
	std::chrono::steady_clock::time_point myInfoAt{};
};
