package shuffle

import (
	"context"
	"fmt"
	"math/rand"
	"strings"
	"testing"

	"github.com/bwmarrin/discordgo"
)

type fakeShuffleMover struct {
	members map[string]*discordgo.Member
	perms   map[string]int64
	moves   []moveCall
	moveErr map[string]error
}

type moveCall struct {
	userID    string
	channelID string
}

func newFakeShuffleMover() *fakeShuffleMover {
	return &fakeShuffleMover{
		members: map[string]*discordgo.Member{},
		perms:   map[string]int64{},
		moveErr: map[string]error{},
	}
}

func (f *fakeShuffleMover) GuildMember(_ string, userID string, _ ...discordgo.RequestOption) (*discordgo.Member, error) {
	member, ok := f.members[userID]
	if !ok {
		return nil, fmt.Errorf("member not found")
	}
	copy := *member
	return &copy, nil
}

func (f *fakeShuffleMover) GuildMemberMove(_ string, userID string, channelID *string, _ ...discordgo.RequestOption) error {
	if err, ok := f.moveErr[userID]; ok && err != nil {
		return err
	}
	call := moveCall{userID: userID}
	if channelID != nil {
		call.channelID = *channelID
	}
	f.moves = append(f.moves, call)
	return nil
}

func (f *fakeShuffleMover) UserChannelPermissions(_ string, channelID string, _ ...discordgo.RequestOption) (int64, error) {
	return f.perms[channelID], nil
}

func newShuffleState(guildID string, channels []*discordgo.Channel, voiceStates []*discordgo.VoiceState, members []*discordgo.Member) *discordgo.State {
	state := discordgo.NewState()
	state.Ready = discordgo.Ready{User: &discordgo.User{ID: "bot"}}
	_ = state.GuildAdd(&discordgo.Guild{ID: guildID, Channels: channels, VoiceStates: voiceStates})
	for _, member := range members {
		_ = state.MemberAdd(member)
	}
	return state
}

func TestEqualBalancesUsersEvenly(t *testing.T) {
	guildID := "g1"
	channels := []*discordgo.Channel{
		{ID: "c1", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
		{ID: "c2", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
		{ID: "c3", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
	}
	voiceStates := []*discordgo.VoiceState{
		{GuildID: guildID, UserID: "u1", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u2", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u3", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u4", ChannelID: "c2"},
		{GuildID: guildID, UserID: "u5", ChannelID: "c2"},
		{GuildID: guildID, UserID: "u6", ChannelID: "c3"},
		{GuildID: guildID, UserID: "u7", ChannelID: "c3"},
	}
	members := []*discordgo.Member{
		{GuildID: guildID, User: &discordgo.User{ID: "u1"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u2"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u3"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u4"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u5"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u6"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u7"}},
	}
	state := newShuffleState(guildID, channels, voiceStates, members)
	mover := newFakeShuffleMover()
	for _, channel := range channels {
		mover.perms[channel.ID] = discordgo.PermissionViewChannel | discordgo.PermissionVoiceConnect | discordgo.PermissionVoiceMoveMembers
	}
	svc := New(state, mover, "bot", rand.New(rand.NewSource(1)))

	result, err := svc.Equal(context.Background(), guildID, []string{"c1", "c2", "c3"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Failures) != 0 {
		t.Fatalf("expected no failures, got %#v", result.Failures)
	}
	if result.MovableUsers != 7 {
		t.Fatalf("movable users = %d, want 7", result.MovableUsers)
	}
	if len(result.ChannelResults) != 3 {
		t.Fatalf("channel results = %d, want 3", len(result.ChannelResults))
	}
	counts := make(map[string]int)
	total := 0
	for _, channel := range result.ChannelResults {
		counts[channel.ChannelID] = channel.Moved
		total += channel.Moved
	}
	if total != 7 {
		t.Fatalf("moved total = %d, want 7", total)
	}
	for _, count := range counts {
		if count < 2 || count > 3 {
			t.Fatalf("counts = %#v, want balanced 3/2/2", counts)
		}
	}
	if len(mover.moves) != 7 {
		t.Fatalf("move calls = %d, want 7", len(mover.moves))
	}
	seen := map[string]struct{}{}
	for _, move := range mover.moves {
		if _, ok := seen[move.userID]; ok {
			t.Fatalf("user moved twice: %s", move.userID)
		}
		seen[move.userID] = struct{}{}
	}
}

func TestEqualErrorsWhenNotEnoughPeople(t *testing.T) {
	guildID := "g1"
	channels := []*discordgo.Channel{
		{ID: "c1", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
		{ID: "c2", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
		{ID: "c3", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
	}
	voiceStates := []*discordgo.VoiceState{
		{GuildID: guildID, UserID: "u1", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u2", ChannelID: "c2"},
	}
	state := newShuffleState(guildID, channels, voiceStates, []*discordgo.Member{
		{GuildID: guildID, User: &discordgo.User{ID: "u1"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u2"}},
	})
	mover := newFakeShuffleMover()
	for _, channel := range channels {
		mover.perms[channel.ID] = discordgo.PermissionViewChannel | discordgo.PermissionVoiceConnect | discordgo.PermissionVoiceMoveMembers
	}
	svc := New(state, mover, "bot", rand.New(rand.NewSource(1)))

	result, err := svc.Equal(context.Background(), guildID, []string{"c1", "c2", "c3"}, nil)
	if err == nil {
		t.Fatal("expected not enough people error")
	}
	if !strings.Contains(err.Error(), "not enough people") {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(mover.moves) != 0 {
		t.Fatalf("expected no moves, got %d", len(mover.moves))
	}
	if result.MovableUsers != 0 {
		t.Fatalf("expected empty result on hard error, got %#v", result)
	}
}

func TestEqualRespectsExclusionsAndBots(t *testing.T) {
	guildID := "g1"
	channels := []*discordgo.Channel{
		{ID: "c1", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
		{ID: "c2", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
	}
	voiceStates := []*discordgo.VoiceState{
		{GuildID: guildID, UserID: "u1", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u2", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u3", ChannelID: "c2"},
		{GuildID: guildID, UserID: "bot-user", ChannelID: "c2"},
	}
	members := []*discordgo.Member{
		{GuildID: guildID, User: &discordgo.User{ID: "u1"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u2"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u3"}},
		{GuildID: guildID, User: &discordgo.User{ID: "bot-user", Bot: true}},
	}
	state := newShuffleState(guildID, channels, voiceStates, members)
	mover := newFakeShuffleMover()
	for _, channel := range channels {
		mover.perms[channel.ID] = discordgo.PermissionViewChannel | discordgo.PermissionVoiceConnect | discordgo.PermissionVoiceMoveMembers
	}
	svc := New(state, mover, "bot", rand.New(rand.NewSource(1)))

	result, err := svc.Equal(context.Background(), guildID, []string{"c1", "c2"}, []string{"u2"})
	if err != nil {
		t.Fatal(err)
	}
	if result.MovableUsers != 2 {
		t.Fatalf("movable users = %d, want 2", result.MovableUsers)
	}
	if result.ExcludedUsers != 1 {
		t.Fatalf("excluded users = %d, want 1", result.ExcludedUsers)
	}
	if len(mover.moves) != 2 {
		t.Fatalf("move calls = %d, want 2", len(mover.moves))
	}
}

func TestEqualRejectsDuplicateChannels(t *testing.T) {
	guildID := "g1"
	channels := []*discordgo.Channel{
		{ID: "c1", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
		{ID: "c2", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
	}
	state := newShuffleState(guildID, channels, nil, nil)
	mover := newFakeShuffleMover()
	for _, channel := range channels {
		mover.perms[channel.ID] = discordgo.PermissionViewChannel | discordgo.PermissionVoiceConnect | discordgo.PermissionVoiceMoveMembers
	}
	svc := New(state, mover, "bot", rand.New(rand.NewSource(1)))

	if _, err := svc.Equal(context.Background(), guildID, []string{"c1", "c1"}, nil); err == nil {
		t.Fatal("expected duplicate channel error")
	}
	if len(mover.moves) != 0 {
		t.Fatalf("expected no moves, got %d", len(mover.moves))
	}
}

func TestEqualKeepsShufflingAfterMoveFailure(t *testing.T) {
	guildID := "g1"
	channels := []*discordgo.Channel{
		{ID: "c1", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
		{ID: "c2", GuildID: guildID, Type: discordgo.ChannelTypeGuildVoice},
	}
	voiceStates := []*discordgo.VoiceState{
		{GuildID: guildID, UserID: "u1", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u2", ChannelID: "c1"},
		{GuildID: guildID, UserID: "u3", ChannelID: "c2"},
	}
	members := []*discordgo.Member{
		{GuildID: guildID, User: &discordgo.User{ID: "u1"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u2"}},
		{GuildID: guildID, User: &discordgo.User{ID: "u3"}},
	}
	state := newShuffleState(guildID, channels, voiceStates, members)
	mover := newFakeShuffleMover()
	for _, channel := range channels {
		mover.perms[channel.ID] = discordgo.PermissionViewChannel | discordgo.PermissionVoiceConnect | discordgo.PermissionVoiceMoveMembers
	}
	mover.moveErr["u2"] = fmt.Errorf("temporary move failure")
	svc := New(state, mover, "bot", rand.New(rand.NewSource(1)))

	result, err := svc.Equal(context.Background(), guildID, []string{"c1", "c2"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if result.MovedUsers != 2 {
		t.Fatalf("moved users = %d, want 2", result.MovedUsers)
	}
	if len(result.Failures) != 1 {
		t.Fatalf("failures = %d, want 1", len(result.Failures))
	}
	if len(mover.moves) != 2 {
		t.Fatalf("move calls = %d, want 2", len(mover.moves))
	}
}

func TestParseExcludedUserIDs(t *testing.T) {
	ids, err := parseExcludedUserIDs("<@123>, 456 <@!789> 456")
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 3 || ids[0] != "123" || ids[1] != "456" || ids[2] != "789" {
		t.Fatalf("unexpected ids: %#v", ids)
	}
	if _, err := parseExcludedUserIDs("abc"); err == nil {
		t.Fatal("expected invalid excluded user error")
	}
}

func TestShuffleApplicationCommandShape(t *testing.T) {
	command := ShuffleApplicationCommand()
	if command.Name != shuffleCommandName {
		t.Fatalf("command name = %q", command.Name)
	}
	if len(command.Options) != 1 || command.Options[0].Name != shuffleEqualGroup {
		t.Fatalf("unexpected command tree: %#v", command.Options)
	}
	group := command.Options[0]
	if len(group.Options) != 3 {
		t.Fatalf("expected 3 equal subcommands, got %d", len(group.Options))
	}
	for _, name := range []string{"two", "three", "four"} {
		found := false
		for _, option := range group.Options {
			if option.Name == name {
				found = true
			}
		}
		if !found {
			t.Fatalf("missing subcommand %s", name)
		}
	}
}
