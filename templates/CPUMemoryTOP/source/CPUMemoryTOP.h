/* Shared Use License: This file is owned by Derivative Inc. (Derivative)
* and can only be used, and/or modified for use, in conjunction with
* Derivative's TouchDesigner software, and only if you are a licensee who has
* accepted Derivative's TouchDesigner license or assignment agreement
* (which also govern the use of this file). You may share or redistribute
* a modified version of this file provided the following conditions are met:
*
* 1. The shared file or redistribution must retain the information set out
* above and this list of conditions.
* 2. Derivative's name (Derivative Inc.) or its trademarks may not be used
* to endorse or promote products derived from this file without specific
* prior written permission from Derivative.
*/

#include "TOP_CPlusPlusBase.h"
#include "FrameQueue.h"
#include <thread>
#include <atomic>
#include <mutex>
#include <condition_variable>
using namespace TD;

// One instance per TOP node. Fills CPU memory and uploads it as the TOP's
// texture (TOP_ExecuteMode::CPUMem). With THREADING_EXAMPLE defined in the .cpp,
// a producer thread fills the buffers; the destructor stops and joins it.
class CPUMemoryTOP : public TOP_CPlusPlusBase
{
public:
	// The TOP_Context is kept to create output buffers (also from the producer thread).
	CPUMemoryTOP(const OP_NodeInfo *info, TOP_Context* context);
	virtual ~CPUMemoryTOP();

	virtual void		getGeneralInfo(TOP_GeneralInfo *, const OP_Inputs*, void*) override;

	virtual void		execute(TOP_Output*,
							const OP_Inputs*,
							void* reserved1) override;

	// Writes one width x height RGBA32Float image at byteOffset. Static and
	// only uses its arguments, so it is safe to call from the producer thread.
	static void			fillBuffer(OP_SmartRef<TOP_Buffer>& mem, uint64_t byteOffset, int width, int height, double step, double brightness);


	virtual int32_t		getNumInfoCHOPChans(void *reserved1) override;
	virtual void		getInfoCHOPChan(int32_t index,
								OP_InfoCHOPChan *chan, void* reserved1) override;

	virtual bool		getInfoDATSize(OP_InfoDATSize *infoSize, void *reserved1) override;
	virtual void		getInfoDATEntries(int32_t index,
									int32_t nEntries,
									OP_InfoDATEntries *entries,
									void *reserved1) override;

	virtual void		setupParameters(OP_ParameterManager *manager, void *reserved1) override;
	virtual void		pulsePressed(const char *name, void *reserved1) override;

	// Producer side of the THREADING_SIGNALED_PRODUCER handshake: blocks until
	// startMoreWork() is called.
	void				waitForMoreWork();

private:

	// Non-threaded path of execute(): create a buffer, fill each layer, upload.
	void				fillAndUpload(TOP_Output* output, double speed, int width, int height, OP_TexDim texDim, int numLayers, int colorBufferIndex);

	// Wakes the producer thread: called by execute() in THREADING_SIGNALED_PRODUCER
	// mode, and by the destructor in every threading mode so a waiting thread can exit.
	void				startMoreWork();
	// We don't need to store this pointer, but we do for the example.
	// The OP_NodeInfo class store information about the node that's using
	// this instance of the class (like its name).
	const OP_NodeInfo*	myNodeInfo;

	// In this example this value will be incremented each time the execute()
	// function is called, then passes back to the TOP
	int					myExecuteCount;

	// mySpeed/myBrightness are written by execute() and read by the producer
	// thread under mySettingsLock. myStep is also read without the lock by the
	// Info CHOP/DAT callbacks, hence atomic; increments/resets take the lock.
	std::mutex			mySettingsLock;
	std::atomic<double>	myStep;
	double				mySpeed;
	double				myBrightness;

	// Used for threading example
	// Search for #define THREADING_EXAMPLE to enable that example
	FrameQueue			myFrameQueue;
	std::thread*		myThread;
	std::atomic<bool>	myThreadShouldExit;

	std::condition_variable	myCondition;
	std::mutex			myConditionLock;
	std::atomic<bool>	myStartWork;

	TOP_Context*		myContext;
	OP_SmartRef<OP_TOPDownloadResult> myPrevDownRes;
};
